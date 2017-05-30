from __future__ import print_function
from collections import namedtuple
import numpy as np
import tensorflow as tf
from model import LSTMPolicy, MetaPolicy
import six.moves.queue as queue
import scipy.signal
import threading
import distutils.version
use_tf12_api = distutils.version.LooseVersion(tf.VERSION) >= distutils.version.LooseVersion('0.12.0')
import cv2


def discount(x, gamma):
    return scipy.signal.lfilter([1], [1, -gamma], x[::-1], axis=0)[::-1]

class A3C(object):
    def __init__(self, env, task, visualise, test=False):
        """
An implementation of the A3C algorithm that is reasonably well-tuned for the VNC environments.
Below, we will have a modest amount of complexity due to the way TensorFlow handles data parallelism.
But overall, we'll define the model, specify its inputs, and describe how the policy gradients step
should be computed.
"""

        self.env = env
        self.task = task
        self.meta_action_size = 32

        worker_device = "/job:worker/task:{}/cpu:0".format(task)
        if test:
           worker_device = "/job:eval/task:{}/cpu:0".format(task)
        with tf.device(tf.train.replica_device_setter(1, worker_device=worker_device)):
            with tf.variable_scope("global"):
                self.network = LSTMPolicy(env.observation_space.shape, env.action_space.n, self.meta_action_size)
                self.global_step = tf.get_variable("global_step", [], tf.int32, initializer=tf.constant_initializer(0, dtype=tf.int32),
                                                   trainable=False)
                self.meta_network = MetaPolicy(env.observation_space.shape, self.meta_action_size)

        with tf.device(worker_device):
            with tf.variable_scope("local"):
                self.local_network = pi = LSTMPolicy(env.observation_space.shape, env.action_space.n, self.meta_action_size)
                self.local_meta_network = meta_pi = MetaPolicy(env.observation_space.shape, self.meta_action_size)
                pi.global_step = self.global_step

            self.ac = tf.placeholder(tf.float32, [None, env.action_space.n], name="ac")
            self.adv = tf.placeholder(tf.float32, [None], name="adv")
            self.r = tf.placeholder(tf.float32, [None], name="r")

            log_prob_tf = tf.nn.log_softmax(pi.logits)
            prob_tf = tf.nn.softmax(pi.logits)

            # the "policy gradients" loss:  its derivative is precisely the policy gradient
            # notice that self.ac is a placeholder that is provided externally.
            # adv will contain the advantages, as calculated in process_rollout
            pi_loss = - tf.reduce_sum(tf.reduce_sum(log_prob_tf * self.ac, [1]) * self.adv)

            # loss of value function
            vf_loss = 0.5 * tf.reduce_sum(tf.square(pi.vf - self.r))
            entropy = - tf.reduce_sum(prob_tf * log_prob_tf)

            bs = tf.to_float(tf.shape(pi.x)[0])
            self.loss = pi_loss + 0.5 * vf_loss - entropy * 0.01


            self.visualise = visualise

            grads = tf.gradients(self.loss, pi.var_list)

            actor_summary = [
                tf.summary.scalar("model/policy_loss", pi_loss / bs),
                tf.summary.scalar("model/value_loss", vf_loss / bs),
                tf.summary.scalar("model/entropy", entropy / bs),
                tf.summary.image("model/state", pi.x),
                tf.summary.scalar("model/grad_global_norm", tf.global_norm(grads)),
                tf.summary.scalar("model/var_global_norm", tf.global_norm(pi.var_list))
                ]

            self.summary_op = tf.summary.merge(actor_summary)

            grads, _ = tf.clip_by_global_norm(grads, 40.0)

            # This is sync ops which copy weights from shared space to the local.
            self.sync = tf.group(
                *(
                    [ v1.assign(v2) for v1, v2 in zip(pi.var_list, self.network.var_list)]
                 ))


            grads_and_vars = list(zip(grads, self.network.var_list))
            inc_step = self.global_step.assign_add(tf.shape(pi.x)[0])
            # each worker has a different set of adam optimizer parameters
            opt = tf.train.AdamOptimizer(1e-4)
            self.train_op = tf.group(opt.apply_gradients(grads_and_vars), inc_step)
            self.summary_writer = None
            self.local_steps = 0


            # sync target ops, copy weight from main network to the target network
            self.target_sync = tf.group(
                *(
                    [ v1.assign(v2) for v1, v2 in zip(pi.target_var_list, self.network.conv_var_list)]
                 ))

            ###################################
            ########## META CONTROLLER ########
            ###################################
            self.meta_ac = tf.placeholder(tf.float32, [None, self.meta_action_size], name="meta_ac")
            self.meta_adv = tf.placeholder(tf.float32, [None], name="meta_adv")
            self.meta_r = tf.placeholder(tf.float32, [None], name="meta_r")

            meta_log_prob_tf = tf.nn.log_softmax(meta_pi.logits)
            meta_prob_tf = tf.nn.softmax(meta_pi.logits)

            meta_pi_loss = - tf.reduce_sum(tf.reduce_sum(meta_log_prob_tf * self.meta_ac, [1]) * self.meta_adv)
            meta_vf_loss = 0.5 * tf.reduce_sum(tf.square(meta_pi.vf - self.meta_r))

            # entropy
            meta_entropy = - tf.reduce_sum(meta_prob_tf * meta_log_prob_tf)
            meta_bs = tf.to_float(tf.shape(meta_pi.x)[0])

            self.meta_loss = meta_pi_loss + 0.5 * meta_vf_loss - meta_entropy * 0.01
            meta_grads = tf.gradients(self.meta_loss, meta_pi.var_list)
            meta_grads, _ = tf.clip_by_global_norm(meta_grads, 40.0)

            self.meta_sync = tf.group(
                *(
                    [ v1.assign(v2) for v1, v2 in zip(meta_pi.var_list, self.meta_network.var_list)]
                 ))

            meta_grads_and_vars = list(zip(meta_grads, self.meta_network.var_list))
            meta_opt = tf.train.AdamOptimizer(1e-4)
            self.meta_train_op = meta_opt.apply_gradients(meta_grads_and_vars)

            meta_summary = [
                tf.summary.scalar("meta_model/policy_loss", meta_pi_loss / meta_bs),
                tf.summary.scalar("meta_model/value_loss", meta_vf_loss / meta_bs),
                tf.summary.scalar("meta_model/entropy", meta_entropy / meta_bs),
                tf.summary.scalar("meta_model/grad_global_norm", tf.global_norm(meta_grads)),
                tf.summary.scalar("meta_model/var_global_norm", tf.global_norm(meta_pi.var_list))
            ]
            self.meta_summary_op = tf.summary.merge(meta_summary)
            self.beta = 0.75
            self.num_sub_loop_per_meta_loop = 1
            self.sub_bptt = 100

    def start(self, sess, summary_writer):
        self.summary_writer = summary_writer

        # Initialise Actor
        # Initialise last_state and last_features
        self.last_state = self.env.reset()
        self.last_features = self.local_network.get_initial_features()
        self.last_action = np.zeros(self.env.action_space.n)
        self.last_reward = [0]
        self.length = 0
        self.rewards = 0
        self.ex_rewards = 0
        self.in_rewards = 0


        # Initialise Meta controller
        self.last_meta_state = self.env.reset()
        self.last_meta_features = self.local_meta_network.get_initial_features()
        self.last_meta_action = np.zeros(self.meta_action_size)
        self.last_meta_reward = [0]

        #
        self.last_conv_feature = np.zeros(self.meta_action_size)

    def process(self, sess):
        # sync target network
        sess.run(self.target_sync)

        num_local_steps = 5
        # run 10000 steps before each target sync
        for _local_step in range(num_local_steps):
            # run 2000 time step
            self.meta_process(sess)

    def meta_process(self, sess):
        """
        Everytime process is called.
        The meta_network get sync.
        The actor_process is run for 20 times.
        The meta_network calculate gradient and update
        """
        sess.run(self.meta_sync)

        terminal_end = False
        # TODO: tune this too
        num_local_steps = 20
        env = self.env
        policy = self.local_meta_network

        states  = []
        actions = []
        rewards = []
        values  = []
        r       = 0.0
        terminal= False
        features= []
        prev_actions = []
        prev_rewards = []

        num_sub_loop_per_meta_loop = self.num_sub_loop_per_meta_loop

        for _local_step in range(num_local_steps):
            fetched = policy.act(self.last_meta_state, self.last_meta_features[0],
                                 self.last_meta_features[1], self.last_meta_action,
                                 self.last_meta_reward)
            action, value_, features_ = fetched[0], fetched[1], fetched[2:]

            reward = 0
            # run actors several times
            # TODO: tune this ... 2? maybe
            for _ in range(num_sub_loop_per_meta_loop):
                state, reward_, terminal, info = self.actor_process(sess, action)
                reward += reward_
                if terminal:
                    break
            # collect experience
            states += [self.last_meta_state]
            actions += [action]
            rewards += [reward]
            values += [value_]
            features += [self.last_meta_features]
            prev_actions += [self.last_meta_action]
            prev_rewards += [self.last_meta_reward]

            # update state
            self.last_meta_state = state
            self.last_meta_features = features_
            self.last_meta_action = action
            self.last_meta_reward = [reward]

            if terminal:
                self.last_meta_features = policy.get_initial_features()
                break
        if not terminal:
            r = policy.value(self.last_meta_state, self.last_meta_features[0],
                                 self.last_meta_features[1], self.last_meta_action,
                                 self.last_meta_reward)

        # Process rollout
        gamma = 0.99
        lambda_ = 1.0
        batch_si = np.asarray(states)
        batch_a = np.asarray(actions)
        rewards_plus_v = np.asarray(rewards + [r])
        rewards = np.asarray(rewards)
        vpred_t = np.asarray(values + [r])
        batch_r = discount(rewards_plus_v, gamma)[:-1]
        delta_t = rewards + gamma * vpred_t[1:] - vpred_t[:-1]
        # this formula for the advantage comes "Generalized Advantage Estimation":
        # https://arxiv.org/abs/1506.02438
        batch_adv = discount(delta_t, gamma * lambda_)
        batch_prev_a = np.asarray(prev_actions)
        batch_prev_r = np.asarray(prev_rewards)
        features = features[0]

        # Gradient Calculation
        fetches = [self.meta_summary_op, self.meta_train_op, self.global_step]

        feed_dict = {
            self.local_meta_network.x: batch_si,
            self.meta_ac: batch_a,
            self.meta_adv: batch_adv,
            self.meta_r: batch_r,
            self.local_meta_network.state_in[0]: features[0],
            self.local_meta_network.state_in[1]: features[1],
            self.local_meta_network.prev_action: batch_prev_a,
            self.local_meta_network.prev_reward: batch_prev_r
        }

        fetched = sess.run(fetches, feed_dict=feed_dict)
        if self.task == 0:
            self.summary_writer.add_summary(tf.Summary.FromString(fetched[0]), fetched[-1])
            self.summary_writer.flush()


    def actor_process(self, sess, meta_action):
        """
        Every time actor_process is called.
        The network get sync.
        The environment is run for 20 steps or until termination.
        The worker calculates gradients and then one update to the shared weight is made.
        (one local step = one update  =< 20 env steps )
        (global step is the number of frames)
        """
        sess.run(self.sync)  # copy weights from shared to local

        # Environment run for 20 steps or less
        terminal_end = False
        num_local_steps = self.sub_bptt
        env = self.env
        policy = self.local_network

        states  = []
        actions = []
        rewards = []
        values  = []
        r       = 0.0
        terminal= False
        features= []
        prev_actions = []
        prev_rewards = []
        extrinsic_rewards = []

        idx = meta_action.argmax()

        for _local_step in range(num_local_steps):
            # Take a step
            fetched = policy.act(self.last_state, self.last_features[0], self.last_features[1],
                                 self.last_action, self.last_reward, meta_action)
            action, value_, features_ = fetched[0], fetched[1], fetched[2:]
            # argmax to convert from one-hot
            state, reward, terminal, info = env.step(action.argmax())

            # clip reward
            reward = min(1, max(-1, reward))

            # Feature control [selectivity (Bengio et al., 2017)]
            conv_feature = policy.get_conv_feature(state)[0][0]
            sel = np.abs(conv_feature[idx] - self.last_conv_feature[idx])
            sel = sel / ( np.sum( np.abs(conv_feature - self.last_conv_feature) ) + 1e-5)
            self.last_conv_feature = conv_feature

            intrinsic_reward = 0.05 * sel

            # record extrinsic reward
            extrinsic_rewards += [reward]
            self.ex_rewards += reward
            self.in_rewards += intrinsic_reward

            # Apply intrinsic reward
            beta = self.beta
            reward = beta * reward + (1.0 - beta) * intrinsic_reward

            if self.visualise:
                vis = state - 0.5 * state * goal_patch + 0.5 * goal_patch
                vis = cv2.resize(vis, (500,500))
                cv2.imshow('img', vis)
                cv2.waitKey(10)

            # collect the experience
            states += [self.last_state]
            actions += [action]
            rewards += [reward]
            values += [value_]
            features += [self.last_features]
            prev_actions += [self.last_action]
            prev_rewards += [self.last_reward]


            self.length += 1
            self.rewards += reward

            self.last_state = state
            self.last_features = features_
            self.last_action = action
            self.last_reward = [reward]

            if info:
                summary = tf.Summary()
                for k, v in info.items():
                    summary.value.add(tag=k, simple_value=float(v))


                self.summary_writer.add_summary(summary, policy.global_step.eval())
                self.summary_writer.flush()

            timestep_limit = env.spec.tags.get('wrapper_config.TimeLimit.max_episode_steps')
            if terminal or self.length >= timestep_limit:
                terminal_end = True
                if self.length >= timestep_limit or not env.metadata.get('semantics.autoreset'):
                    self.last_state = env.reset()
                self.last_features = policy.get_initial_features()
                print("Episode finished. Sum of rewards: %d. Length: %d" % (self.rewards, self.length))

                summary = tf.Summary()
                summary.value.add(tag='global/episode_shaped_reward', simple_value=self.rewards)
                summary.value.add(tag='global/shaped_reward_per_time', simple_value=self.rewards/self.length)
                summary.value.add(tag='global/episode_extrinsic_reward', simple_value=self.ex_rewards)
                summary.value.add(tag='global/episode_intrinsic_reward', simple_value=self.in_rewards)

                self.summary_writer.add_summary(summary, policy.global_step.eval())
                self.summary_writer.flush()

                self.length = 0
                self.rewards = 0
                self.ex_rewards = 0
                self.in_rewards = 0

                break

        if not terminal_end:
            r = policy.value(self.last_state, self.last_features[0],
                             self.last_features[1], self.last_action,
                             self.last_reward, meta_action)

        # Process rollout
        gamma = 0.99
        lambda_ = 1.0
        batch_si = np.asarray(states)
        batch_a = np.asarray(actions)
        rewards_plus_v = np.asarray(rewards + [r])
        rewards = np.asarray(rewards)
        vpred_t = np.asarray(values + [r])
        batch_r = discount(rewards_plus_v, gamma)[:-1]
        delta_t = rewards + gamma * vpred_t[1:] - vpred_t[:-1]
        # this formula for the advantage comes "Generalized Advantage Estimation":
        # https://arxiv.org/abs/1506.02438
        batch_adv = discount(delta_t, gamma * lambda_)
        batch_prev_a = np.asarray(prev_actions)
        batch_prev_r = np.asarray(prev_rewards)
        features = features[0] # only use first feature into dynamic rnn


        # Batch meta action
        batch_meta_ac = np.repeat([meta_action], len(batch_si), axis=0)

        # Gradient Calculation
        should_compute_summary = self.task == 0 and self.local_steps % 11 == 0
        if should_compute_summary:
            fetches = [self.summary_op, self.train_op, self.global_step]
        else:
            fetches = [self.train_op, self.global_step]



        feed_dict = {
            self.local_network.x: batch_si,
            self.ac: batch_a,
            self.adv: batch_adv,
            self.r: batch_r,
            self.local_network.state_in[0]: features[0],
            self.local_network.state_in[1]: features[1],
            self.local_network.prev_action: batch_prev_a,
            self.local_network.prev_reward: batch_prev_r,
            self.local_network.meta_action: batch_meta_ac
        }

        fetched = sess.run(fetches, feed_dict=feed_dict)

        if should_compute_summary:
            self.summary_writer.add_summary(tf.Summary.FromString(fetched[0]), fetched[-1])
            self.summary_writer.flush()
        self.local_steps += 1

        # discount extrinsic reward for the meta controller
        #gamma = 0.99
        # early rewards are better?
        #discount_filter = np.array([gamma**i for i in range(len(extrinsic_rewards))])
        #extrinsic_reward = np.sum(discount_filter * extrinsic_rewards)

        return self.last_state, np.sum(extrinsic_rewards), terminal_end, None

    def evaluate(self,sess):

        global_step = sess.run(self.global_step)
        sess.run(self.meta_sync)
        sess.run(self.sync)

        meta_policy = self.local_meta_network
        policy = self.local_network
        env = self.env
        rewards_stat = []
        length_stat = []
        # average over 100 episode?
        for episode in range(100):
            terminal = False

            last_state = env.reset()
            last_meta_state = last_state
            last_features = policy.get_initial_features()
            last_meta_features = meta_policy.get_initial_features()
            last_meta_action = np.zeros(self.meta_action_size)
            last_meta_reward = [0]
            last_action = np.zeros(self.env.action_space.n)
            last_reward = [0]
            rewards = 0
            length = 0
            last_conv_feature = np.zeros(self.meta_action_size)


            while not terminal:

                fetched = meta_policy.act(last_meta_state, last_meta_features[0],
                                          last_meta_features[1], last_meta_action, last_meta_reward)
                meta_action, meta_value_, meta_features_ = fetched[0], fetched[1], fetched[2:]

                meta_reward = 0

                idx = meta_action.argmax()

                for _ in range(self.sub_bptt*self.num_sub_loop_per_meta_loop):
                    fetched = policy.act(last_state, last_features[0], last_features[1],
                                     last_action, last_reward, meta_action)
                    action, value_, features_ = fetched[0], fetched[1], fetched[2:]
                    state, reward, terminal, info = env.step(action.argmax())

                    if self.visualise:
                        vis = cv2.resize(state , (500,500))
                        cv2.imshow('img', vis)
                        cv2.waitKey(10)

                    env_reward = reward

                    # clip reward
                    reward = min(1, max(-1, reward))

                    # Feature control [selectivity (Bengio et al., 2017)]
                    conv_feature = policy.get_conv_feature(state)[0][0]
                    sel = np.abs(conv_feature[idx] - last_conv_feature[idx])
                    sel = sel / ( np.sum( np.abs(conv_feature - last_conv_feature) ) + 1e-5)
                    last_conv_feature = conv_feature

                    intrinsic_reward = 0.05 * sel

                    # Apply intrinsic reward
                    beta = self.beta
                    shaped_reward = beta * reward + (1.0 - beta) * intrinsic_reward


                    length += 1
                    rewards += env_reward
                    last_state = state
                    last_features = features_
                    last_action = action
                    last_reward = [shaped_reward]
                    meta_reward += reward

                    timestep_limit = env.spec.tags.get('wrapper_config.TimeLimit.max_episode_steps')
                    if terminal or length >= timestep_limit:
                        terminal = True
                        break

                last_meta_state = last_state
                last_meta_features = meta_features_
                last_meta_action = meta_action
                last_meta_reward = [meta_reward]

                if terminal:
                    break

            rewards_stat.append(rewards)
            length_stat.append(length)

        summary = tf.Summary()
        summary.value.add(tag='Eval/Average_Reward', simple_value=np.mean(rewards_stat))
        summary.value.add(tag='Eval/SD_Reward', simple_value=np.std(rewards_stat))
        summary.value.add(tag='Eval/Average_Lenght', simple_value=np.mean(length_stat))
        self.summary_writer.add_summary(summary, global_step)
        self.summary_writer.flush()
