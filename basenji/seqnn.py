# Copyright 2017 Calico LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =========================================================================

from __future__ import print_function

import gc
import sys
import time

import numpy as np
import tensorflow as tf
from tensorflow.contrib.framework.python.ops import create_global_step

from basenji.dna_io import hot1_rc
import basenji.ops

class SeqNN:
    def __init__(self):
        pass

    def build(self, job):
        ###################################################
        # model parameters and placeholders
        ###################################################
        self.set_params(job)

        # batches
        self.inputs = tf.placeholder(tf.float32, shape=(self.batch_size, self.batch_length, self.seq_depth), name='inputs')
        if self.target_classes == 1:
            self.targets = tf.placeholder(tf.float32, shape=(self.batch_size, self.batch_length//self.target_pool, self.num_targets), name='targets')
        else:
            self.targets = tf.placeholder(tf.int32, shape=(self.batch_size, self.batch_length//self.target_pool, self.num_targets), name='targets')
        self.targets_na = tf.placeholder(tf.bool, shape=(self.batch_size, self.batch_length//self.target_pool))

        print('Targets pooled by %d to length %d' % (self.target_pool, self.batch_length//self.target_pool))

        # dropout rates
        self.cnn_dropout_ph = []
        for li in range(self.cnn_layers):
            self.cnn_dropout_ph.append(tf.placeholder(tf.float32))
        self.full_dropout_ph = []
        for li in range(self.full_layers):
            self.full_dropout_ph.append(tf.placeholder(tf.float32))

        if self.batch_renorm:
            create_global_step()
            RMAX_decay = basenji.ops.adjust_max(6000, 60000, 1, 3, name='RMAXDECAY')
            DMAX_decay = basenji.ops.adjust_max(6000, 60000, 0, 5, name='DMAXDECAY')
            renorm_clipping = {'rmin':1./RMAX_decay, 'rmax':RMAX_decay, 'dmax':DMAX_decay}
        else:
            renorm_clipping = {}

        # training conditional
        self.is_training = tf.placeholder(tf.bool)

        ###################################################
        # convolution layers
        ###################################################
        seq_length = self.batch_length
        seq_depth = self.seq_depth

        weights_regularizers = 0
        self.layer_reprs = []
        self.filter_weights = []

        if self.save_reprs:
            self.layer_reprs.append(self.inputs)

        # reshape for convolution
        # seqs_repr = tf.reshape(self.inputs, [self.batch_size, 1, seq_length, seq_depth])
        seqs_repr = self.inputs

        for li in range(self.cnn_layers):
            with tf.variable_scope('cnn%d' % li) as vs:

                seqs_repr_next = tf.layers.conv1d(seqs_repr, filters=self.cnn_filters[li], kernel_size=[self.cnn_filter_sizes[li]], strides=self.cnn_strides[li], padding='same', dilation_rate=[self.cnn_dilation[li]], use_bias=False, kernel_initializer=tf.contrib.layers.xavier_initializer(), kernel_regularizer=None)
                print('Convolution w/ %d %dx%d filters strided %d, dilated %d' % (self.cnn_filters[li], seq_depth, self.cnn_filter_sizes[li], self.cnn_strides[li], self.cnn_dilation[li]))

                # regularize
                # if self.cnn_l2[li] > 0:
                #    weights_regularizers += self.cnn_l2[li]*tf.reduce_mean(tf.nn.l2_loss(kernel))

                # maintain a pointer to the weights
                # self.filter_weights.append(kernel)

                # batch normalization
                seqs_repr_next = tf.layers.batch_normalization(seqs_repr_next, momentum=0.9, training=self.is_training, renorm=self.batch_renorm, renorm_clipping=renorm_clipping, renorm_momentum=0.9)
                print('Batch normalization')

                # ReLU
                seqs_repr_next = tf.nn.relu(seqs_repr_next)
                print('ReLU')

                # pooling
                if self.cnn_pool[li] > 1:
                    seqs_repr_next = tf.layers.max_pooling1d(seqs_repr_next, pool_size=self.cnn_pool[li], strides=self.cnn_pool[li], padding='same')
                    print('Max pool %d' % self.cnn_pool[li])

                # dropout
                if self.cnn_dropout[li] > 0:
                    seqs_repr_next = tf.nn.dropout(seqs_repr_next, 1.0-self.cnn_dropout_ph[li])
                    # seqs_repr = tf.layers.dropout(seqs_repr, rate=self.cnn_dropout[li], training=self.is_training)
                    print('Dropout w/ probability %.3f' % self.cnn_dropout[li])

                # updates size variables
                seq_length = seq_length // self.cnn_pool[li]

                if self.cnn_dense[li]:
                    # concat layer repr
                    seqs_repr = tf.concat(values=[seqs_repr, seqs_repr_next], axis=2)

                    # update size variables
                    seq_depth += self.cnn_filters[li]
                else:
                    # update layer repr
                    seqs_repr = seqs_repr_next

                    # update size variables
                    seq_depth = self.cnn_filters[li]

                # save representation (not positive about this one)
                if self.save_reprs:
                    self.layer_reprs.append(seqs_repr)

        # update batch buffer to reflect pooling
        pool_preds = self.batch_length // seq_length
        if self.batch_buffer % pool_preds != 0:
            print('Please make the batch_buffer %d divisible by the CNN pooling %d' % (self.batch_buffer, pool_preds), file=sys.stderr)
            exit(1)
        self.batch_buffer_pool = self.batch_buffer // pool_preds


        ###################################################
        # fully connected layers
        ###################################################

        # slice out buffer regions
        seqs_repr = seqs_repr[:,self.batch_buffer_pool:seq_length-self.batch_buffer_pool,:]
        seq_length -= 2*self.batch_buffer_pool

        # reshape to make every position an element
        seqs_repr = tf.reshape(seqs_repr, (self.batch_size*seq_length, seq_depth))

        for li in range(self.full_layers):
            with tf.variable_scope('full%d' % li):
                # linear transform
                full_weights = tf.get_variable(name='weights', shape=[seq_depth, self.full_units[li]], dtype=tf.float32, initializer=tf.contrib.layers.xavier_initializer(uniform=True))
                full_biases = tf.Variable(tf.zeros(self.full_units), name='bias')

                # regularize
                if self.full_l2[li] > 0:
                    weights_regularizers += self.full_l2[li]*tf.reduce_mean(tf.nn.l2_loss(full_weights))

                seqs_repr = tf.matmul(seqs_repr, full_weights) + full_biases
                print('Linear transformation %dx%d' % (seq_depth, self.full_units[li]))

                # batch normalization
                seqs_repr = tf.layers.batch_normalization(seqs_repr, momentum=0.9, training=self.is_training, renorm=self.batch_renorm, renorm_clipping=renorm_clipping, renorm_momentum=0.9)
                print('Batch normalization')

                # ReLU
                seqs_repr = tf.nn.relu(seqs_repr)
                print('ReLU')

                # dropout
                if self.full_dropout[li] > 0:
                    seqs_repr = tf.nn.dropout(seqs_repr, 1.0-self.full_dropout_ph[li])
                    print('Dropout w/ probability %.3f' % self.full_dropout[li])

                # update
                seq_depth = self.full_units[li]


        ###################################################
        # final layer
        ###################################################

        with tf.variable_scope('final'):
            # linear transform
            final_weights = tf.get_variable(name='weights', shape=[seq_depth, self.num_targets*self.target_classes], dtype=tf.float32, initializer=tf.contrib.layers.xavier_initializer(uniform=True))
            final_biases = tf.Variable(tf.zeros(self.num_targets*self.target_classes), name='bias')

            if self.final_l1 > 0:
                weights_regularizers += self.final_l1*tf.reduce_mean(tf.abs(final_weights))

            seqs_repr = tf.matmul(seqs_repr, final_weights) + final_biases
            print('Linear transform %dx%dx%d' % (seq_depth, self.num_targets, self.target_classes))

        # expand length back out
        if self.target_classes == 1:
            seqs_repr = tf.reshape(seqs_repr, (self.batch_size, seq_length, self.num_targets))
        else:
            seqs_repr = tf.reshape(seqs_repr, (self.batch_size, seq_length, self.num_targets, self.target_classes))

        seqs_repr = tf.check_numerics(seqs_repr, 'Invalid predictions', name='preds_check')

        # repeat if pooling
        # pool_repeat = pool_preds // self.target_pool
        # if pool_repeat > 1:
        #     tlength = (self.batch_length-2*self.batch_buffer) // self.target_pool
        #     self.preds_op = tf.reshape(tf.tile(tf.reshape(self.preds_op, (-1,self.num_targets)), (1,pool_repeat)), (self.batch_size, tlength, self.num_targets))

        ###################################################
        # loss and optimization
        ###################################################

        # slice out buffer regions
        tstart = self.batch_buffer // self.target_pool
        tend = (self.batch_length - self.batch_buffer) // self.target_pool
        self.targets_op = tf.identity(self.targets[:,tstart:tend,:], name='targets_op')

        # work-around for specifying my own predictions
        self.preds_adhoc = tf.placeholder(tf.float32, shape=seqs_repr.get_shape())

        # choose link
        if self.link in ['identity','linear']:
            self.preds_op = tf.identity(seqs_repr, name='preds')

        elif self.link == 'relu':
            self.preds_op = tf.relu(seqs_repr, name='preds')

        elif self.link == 'exp':
            self.preds_op = tf.exp(tf.clip_by_value(seqs_repr,-50,50), name='preds')

        elif self.link == 'exp_linear':
            self.preds_op = tf.where(seqs_repr > 0, seqs_repr + 1, tf.exp(tf.clip_by_value(seqs_repr,-50,50)), name='preds')

        elif self.link == 'softplus':
            self.preds_op = tf.nn.softplus(seqs_repr, name='preds')

        elif self.link == 'softmax':
            # performed in the loss function, but saving probabilities
            self.preds_prob = tf.nn.softmax(seqs_repr, name='preds')

        else:
            print('Unknown link function %s' % self.link, file=sys.stderr)
            exit(1)

        # choose loss
        if self.loss == 'gaussian':
            self.loss_op = tf.squared_difference(self.preds_op, self.targets_op)
            self.loss_adhoc = tf.squared_difference(self.preds_adhoc, self.targets_op)

        elif self.loss == 'poisson':
            self.loss_op = tf.nn.log_poisson_loss(self.targets_op, tf.log(self.preds_op), compute_full_loss=True)
            self.loss_adhoc = tf.nn.log_poisson_loss(self.targets_op, tf.log(self.preds_adhoc), compute_full_loss=True)

        elif self.loss == 'negative_binomial':
            # define overdispersion alphas
            self.alphas = tf.get_variable('alphas', shape=[self.num_targets], initializer=tf.constant_initializer(-5), dtype=tf.float32)
            self.alphas = tf.nn.softplus(tf.clip_by_value(self.alphas,-50,50))
            tf.summary.histogram('alphas', self.alphas)
            for ti in np.linspace(0,self.num_targets-1,10).astype('int'):
                tf.summary.scalar('alpha_t%d'%ti, self.alphas[ti])

            # compute w/ inverse
            k = 1. / self.alphas

            # expand k
            k_expand = tf.tile(k, [self.batch_size*seq_length])
            k_expand = tf.reshape(k_expand, (self.batch_size, seq_length, self.num_targets))

            # expand lgamma(k)
            lgk_expand = tf.tile(tf.lgamma(k), [self.batch_size*seq_length])
            lgk_expand = tf.reshape(lgk_expand, (self.batch_size, seq_length, self.num_targets))

            # construct loss
            loss1 = self.targets_op * tf.log(self.preds_op / (self.preds_op + k_expand))
            loss2 = k_expand * tf.log(k_expand / (self.preds_op + k_expand))
            loss3 = tf.lgamma(self.targets_op + k_expand) - lgk_expand
            self.loss_op = -(loss1 + loss2 + loss3)

            # adhoc
            loss1 = self.targets_op * tf.log(self.preds_adhoc / (self.preds_adhoc + k_expand))
            loss2 = k_expand * tf.log(k_expand / (self.preds_adhoc + k_expand))
            self.loss_adhoc = -(loss1 + loss2 + loss3)

        elif self.loss == 'negative_binomial_hilbe':
            # define overdispersion alphas
            self.alphas = tf.get_variable('alphas', shape=[self.num_targets], initializer=tf.constant_initializer(-5), dtype=tf.float32)
            self.alphas = tf.exp(tf.clip_by_value(self.alphas,-50,50))

            # expand
            alphas_expand = tf.tile(self.alphas, [self.batch_size*seq_length])
            alphas_expand = tf.reshape(alphas_expand, (self.batch_size, seq_length, self.num_targets))

            # construct loss
            loss1 = self.targets_op * tf.log(self.preds_op)
            loss2 = (alphas_expand * self.targets_op + 1) / alphas_expand
            loss3 = tf.log(alphas_expand * self.preds_op + 1)
            self.loss_op = -loss1 + loss2*loss3

            # adhoc
            loss1 = self.targets_op * tf.log(self.preds_adhoc)
            loss3 = tf.log(alphas_expand * self.preds_adhoc + 1)
            self.loss_adhoc = -loss1 + loss2*loss3

        elif self.loss == 'gamma':
            # jchan document
            self.loss_op = self.targets_op / self.preds_op + tf.log(self.preds_op)
            self.loss_adhoc = self.targets_op / self.preds_adhoc + tf.log(self.preds_adhoc)

        elif self.loss == 'cross_entropy':
            self.loss_op = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=(self.targets_op-1), logits=self.preds_op)
            self.loss_adhoc = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=(self.targets_op-1), logits=self.preds_adhoc)

        else:
            print('Cannot identify loss function %s' % self.loss)
            exit(1)

        # set NaN's to zero
        # self.loss_op = tf.boolean_mask(self.loss_op, tf.logical_not(self.targets_na[:,tstart:tend]))

        self.loss_op = tf.check_numerics(self.loss_op, 'Invalid loss', name='loss_check')

        # reduce lossses by batch and position
        self.loss_op = tf.reduce_mean(self.loss_op, axis=[0,1], name='target_loss')
        self.loss_adhoc = tf.reduce_mean(self.loss_adhoc, axis=[0,1], name='target_loss_adhoc')
        tf.summary.histogram('target_loss', self.loss_op)
        for ti in np.linspace(0,self.num_targets-1,10).astype('int'):
            tf.summary.scalar('loss_t%d'%ti, self.loss_op[ti])
        self.target_losses = self.loss_op
        self.target_losses_adhoc = self.loss_adhoc

        # define target sigmas
        '''
        self.target_sigmas = tf.get_variable('target_sigmas', shape=[self.num_targets], initializer=tf.constant_initializer(2), dtype=tf.float32)
        self.target_sigmas = tf.nn.softplus(tf.clip_by_value(self.target_sigmas,-50,50))
        tf.summary.histogram('target_sigmas', self.target_sigmas)
        for ti in np.linspace(0,self.num_targets-1,10).astype('int'):
            tf.summary.scalar('sigma_t%d'%ti, self.target_sigmas[ti])
        # self.target_sigmas = tf.ones(self.num_targets) / 2.
        '''

        # dot losses target sigmas
        # self.loss_op = self.loss_op / (2*self.target_sigmas)
        # self.loss_adhoc = self.loss_adhoc / (2*self.target_sigmas)

        # fully reduce
        self.loss_op = tf.reduce_mean(self.loss_op, name='loss')
        self.loss_adhoc = tf.reduce_mean(self.loss_adhoc, name='loss_adhoc')

        # add extraneous terms
        self.loss_op += weights_regularizers # + tf.reduce_mean(tf.log(self.target_sigmas))
        self.loss_adhoc += weights_regularizers # + tf.reduce_mean(tf.log(self.target_sigmas))

        # track
        tf.summary.scalar('loss', self.loss_op)

        # define optimization
        if self.optimization == 'adam':
            self.opt = tf.train.AdamOptimizer(self.learning_rate, beta1=self.adam_beta1, beta2=self.adam_beta2, epsilon=self.adam_eps)
        elif self.optimization == 'rmsprop':
            self.opt = tf.train.RMSPropOptimizer(self.learning_rate, decay=self.decay, momentum=self.momentum)
        elif self.optimization in ['sgd','momentum']:
            self.opt = tf.train.MomentumOptimizer(self.learning_rate, momentum=self.momentum)
        else:
            print('Cannot recognize optimization algorithm %s' % self.optimization)
            exit(1)

        # clip gradients
        self.gvs = self.opt.compute_gradients(self.loss_op, aggregation_method=tf.AggregationMethod.EXPERIMENTAL_ACCUMULATE_N)
        if self.grad_clip is None:
            clip_gvs =  self.gvs
        else:
            # batch norm introduces these None values that we have to dodge
            clip_gvs = []
            for i in range(len(self.gvs)):
                g,v = self.gvs[i]
                if g is None:
                    clip_gvs.append(self.gvs[i])
                else:
                    # clip_gvs.append((tf.clip_by_value(g, -self.grad_clip, self.grad_clip), v))
                    clip_gvs.append((tf.clip_by_norm(g, self.grad_clip), v))

        # apply gradients
        self.step_op = self.opt.apply_gradients(clip_gvs)

        # batch norm helper
        # if self.batch_renorm:
        self.update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)


        # summary
        self.merged_summary = tf.summary.merge_all()

        # initialize steps
        self.step = 0


    def drop_rate(self, drop_mult=0.5):
        ''' Drop the optimizer learning rate. '''
        self.opt._lr *= drop_mult


    def gradients(self, sess, batcher, target_indexes=None, layers=None, return_preds=False):
        ''' Compute predictions on a test set.

        In
         sess: TensorFlow session
         batcher: Batcher class with sequence(s)
         target_indexes: Optional target subset list
         layers: Optional layer subset list

        Out
         grads: [S (sequences) x Li (layer i shape) x T (targets) array] * (L layers)
         preds:
        '''

        # initialize target_indexes
        if target_indexes is None:
            target_indexes = np.array(range(self.num_targets))
        elif type(target_indexes) != np.ndarray:
            target_indexes = np.array(target_indexes)

        # initialize gradients
        #  (I need a list for layers because the sizes are different within)
        #  (I'm using a list for targets because I need to compute them individually)
        layer_grads = []
        for lii in range(len(layers)):
            layer_grads.append([])
            for tii in range(len(target_indexes)):
                layer_grads[lii].append([])

        # initialize layers
        if layers is None:
            layers = range(1+self.cnn_layers)
        elif type(layers) != list:
            layers = [layers]

        # initialize predictions
        preds = None
        if return_preds:
            # determine non-buffer region
            buf_start = self.batch_buffer // self.target_pool
            buf_end = (self.batch_length - self.batch_buffer) // self.target_pool
            buf_len = buf_end - buf_start

            # initialize predictions
            preds = np.zeros((batcher.num_seqs, buf_len, len(target_indexes)), dtype='float16')

            # sequence index
            si = 0

        # setup feed dict for dropout
        fd = self.set_mode('test')

        # get first batch
        Xb, _, _, Nb = batcher.next()

        while Xb is not None:
            # update feed dict
            fd[self.inputs] = Xb

            # predict
            preds_batch = sess.run(self.preds_op, feed_dict=fd)

            # compute gradients for each target individually
            for tii in range(len(target_indexes)):
                ti = target_indexes[tii]

                # compute gradients over all positions
                grads_op = tf.gradients(self.preds_op[:,:,ti], [self.layer_reprs[li] for li in layers])
                grads_batch_raw = sess.run(grads_op, feed_dict=fd)

                for lii in range(len(layers)):
                    # clean up
                    grads_batch = grads_batch_raw[lii][:Nb].astype('float16')
                    if grads_batch.shape[1] == 1:
                        grads_batch = grads_batch.squeeze(axis=1)

                    # save
                    layer_grads[lii][tii].append(grads_batch)

            if return_preds:
                # filter for specific targets
                if target_indexes is not None:
                    preds_batch = preds_batch[:,:,target_indexes]

                # accumulate predictions
                preds[si:si+Nb,:,:] = preds_batch[:Nb,:,:]

                # update sequence index
                si += Nb

            # next batch
            Xb, _, _, Nb = batcher.next()

        # reset training batcher
        batcher.reset()

        # stack into arrays
        for lii in range(len(layers)):
            for tii in range(len(target_indexes)):
                # stack sequences
                layer_grads[lii][tii] = np.vstack(layer_grads[lii][tii])

            # transpose targets to back
            layer_grads[lii] = np.array(layer_grads[lii])
            if layer_grads[lii].ndim == 4:
                # length dimension
                layer_grads[lii] = np.transpose(layer_grads[lii], [1,2,3,0])
            else:
                # no length dimension
                layer_grads[lii] = np.transpose(layer_grads[lii], [1,2,0])

        if return_preds:
            return layer_grads, preds
        else:
            return layer_grads


    def gradients_pos(self, sess, batcher, position_indexes, target_indexes=None, layers=None, return_preds=False):
        ''' Compute predictions on a test set.

        In
         sess: TensorFlow session
         batcher: Batcher class with sequence(s)
         position_indexes: Optional position subset list
         target_indexes: Optional target subset list
         layers: Optional layer subset list

        Out
         grads: [S (sequences) x Li (layer i shape) x T (targets) array] * (L layers)
         preds:
        '''

        # initialize target_indexes
        if target_indexes is None:
            target_indexes = np.array(range(self.num_targets))
        elif type(target_indexes) != np.ndarray:
            target_indexes = np.array(target_indexes)

        # initialize layers
        if layers is None:
            layers = range(1+self.cnn_layers)
        elif type(layers) != list:
            layers = [layers]

        # initialize gradients
        #  (I need a list for layers because the sizes are different within)
        #  (I'm using a list for positions/targets because I don't know the downstream object size)
        layer_grads = []
        for lii in range(len(layers)):
            layer_grads.append([])
            for pii in range(len(position_indexes)):
                layer_grads[lii].append([])
                for tii in range(len(target_indexes)):
                    layer_grads[lii][pii].append([])

        # initialize layer reprs
        layer_reprs = []
        for lii in range(len(layers)):
            layer_reprs.append([])

        # initialize predictions
        preds = None
        if return_preds:
            # determine non-buffer region
            buf_start = self.batch_buffer // self.target_pool
            buf_end = (self.batch_length - self.batch_buffer) // self.target_pool
            buf_len = buf_end - buf_start

            # initialize predictions
            preds = np.zeros((batcher.num_seqs, buf_len, len(target_indexes)), dtype='float16')

            # sequence index
            si = 0

        # setup feed dict for dropout
        fd = self.set_mode('test')

        # get first batch
        Xb, _, _, Nb = batcher.next()

        while Xb is not None:
            # update feed dict
            fd[self.inputs] = Xb

            # predict (allegedly takes zero time beyond the first sequence?)
            reprs_batch_raw, preds_batch = sess.run([self.layer_reprs, self.preds_op], feed_dict=fd)

            # clean up layer repr
            reprs_batch = reprs_batch_raw[layers[lii]][:Nb].astype('float16')
            if reprs_batch.shape[1] == 1:
                reprs_batch = reprs_batch.squeeze(axis=1)

            # save repr
            layer_reprs[lii].append(reprs_batch)

            # for each target
            t0 = time.time()
            for tii in range(len(target_indexes)):
                ti = target_indexes[tii]

                # for each position
                for pii in range(len(position_indexes)):
                    pi = position_indexes[pii]

                    # adjust for buffer
                    pi -= self.batch_buffer//self.target_pool

                    # compute gradients
                    grads_op = tf.gradients(self.preds_op[:,pi,ti], [self.layer_reprs[li] for li in layers])
                    grads_batch_raw = sess.run(grads_op, feed_dict=fd)

                    for lii in range(len(layers)):
                        # clean up
                        grads_batch = grads_batch_raw[lii][:Nb].astype('float16')
                        if grads_batch.shape[1] == 1:
                            grads_batch = grads_batch.squeeze(axis=1)

                        # save
                        layer_grads[lii][pii][tii].append(grads_batch)

            if return_preds:
                # filter for specific targets
                if target_indexes is not None:
                    preds_batch = preds_batch[:,:,target_indexes]

                # accumulate predictions
                preds[si:si+Nb,:,:] = preds_batch[:Nb,:,:]

                # update sequence index
                si += Nb

            # next batch
            Xb, _, _, Nb = batcher.next()

        # reset training batcher
        batcher.reset()
        gc.collect()

        # stack into arrays
        for lii in range(len(layers)):
            layer_reprs[lii] = np.vstack(layer_reprs[lii])

            for pii in range(len(position_indexes)):
                for tii in range(len(target_indexes)):
                    # stack sequences
                    layer_grads[lii][pii][tii] = np.vstack(layer_grads[lii][pii][tii])

            # collapse position into arrays
            layer_grads[lii] = np.array(layer_grads[lii])

            # transpose positions and targets to back
            if layer_grads[lii].ndim == 5:
                # length dimension
                layer_grads[lii] = np.transpose(layer_grads[lii], [2, 3, 4, 0, 1])
            else:
                # no length dimension
                layer_grads[lii] = np.transpose(layer_grads[lii] [2, 3, 0, 1])

        if return_preds:
            return layer_grads, layer_reprs, preds
        else:
            return layer_grads, layer_reprs


    def hidden(self, sess, batcher, layers=None):
        ''' Compute hidden representations for a test set. '''

        if layers is None:
            layers = list(range(self.cnn_layers))

        # initialize layer representation data structure
        layer_reprs = []
        for li in range(1+np.max(layers)):
            layer_reprs.append([])
        preds = []

        # setup feed dict
        fd = self.set_mode('test')

        # get first batch
        Xb, _, _, Nb = batcher.next()

        while Xb is not None:
            # update feed dict
            fd[self.inputs] = Xb

            # compute predictions
            layer_reprs_batch, preds_batch = sess.run([self.layer_reprs, self.preds_op], feed_dict=fd)

            # accumulate representations
            for li in layers:
                # squeeze (conv_2d-expanded) second dimension
                if layer_reprs_batch[li].shape[1] == 1:
                    layer_reprs_batch[li] = layer_reprs_batch[li].squeeze(axis=1)

                # append
                layer_reprs[li].append(layer_reprs_batch[li][:Nb].astype('float16'))

            # accumualte predictions
            preds.append(preds_batch[:Nb])

            # next batch
            Xb, _, _, Nb = batcher.next()

        # reset batcher
        batcher.reset()

        # accumulate representations
        for li in layers:
            layer_reprs[li] = np.vstack(layer_reprs[li])

        preds = np.vstack(preds)

        return layer_reprs, preds


    def predict(self, sess, batcher, rc_avg=False, mc_n=0, target_indexes=None, return_var=False, return_all=False, down_sample=1):
        ''' Compute predictions on a test set.

        In
         sess:           TensorFlow session
         batcher:        Batcher class with transcript-covering sequences
         rc_avg:         Average predictions from the forward and reverse complement sequences
         mc_n:           Monte Carlo iterations
         target_indexes: Optional target subset list
         return_var:     Return variance estimates
         down_sample:    Int specifying to consider uniformly spaced sampled positions

        Out
         preds: S (sequences) x L (unbuffered length) x T (targets) array
        '''

        # determine non-buffer region
        buf_start = self.batch_buffer // self.target_pool
        buf_end = (self.batch_length - self.batch_buffer) // self.target_pool
        buf_len = buf_end - buf_start

        # uniformly sample indexes
        ds_indexes = np.arange(0, buf_len, down_sample)

        # initialize prediction arrays
        num_targets = self.num_targets
        if target_indexes is not None:
            num_targets = len(target_indexes)

        preds = np.zeros((batcher.num_seqs, len(ds_indexes), num_targets), dtype='float16')
        if mc_n > 0 and return_var:
            preds_var = np.zeros((batcher.num_seqs, len(ds_indexes), num_targets), dtype='float16')
        if mc_n > 0 and return_all:
            preds_all = np.zeros((batcher.num_seqs, len(ds_indexes), num_targets, mc_n), dtype='float16')

        si = 0

        if mc_n > 0:
            # setup feed dict
            fd = self.set_mode('test_mc')

            # divide iterations between forward and reverse
            mcf_n = mc_n
            if rc_avg:
                mcr_n = mc_n // 2
                mcf_n = mc_n - mcr_n
        else:
            # setup feed dict
            fd = self.set_mode('test')

        # get first batch
        Xb, _, _, Nb = batcher.next()

        while Xb is not None:
            # update feed dict
            fd[self.inputs] = Xb

            # compute predictions
            preds_batch = sess.run(self.preds_op, feed_dict=fd)[:,ds_indexes,:]
            if return_var:
                preds_batch_var = np.zeros(preds_batch.shape, dtype='float32')
            if return_all:
                preds_all[si:si+Nb,:,:,0] = preds_batch[:Nb,:,target_indexes]

            if mc_n > 0:
                # accumulate predictions
                for mi in range(1,mcf_n):
                    preds_i = sess.run(self.preds_op, feed_dict=fd)[:,ds_indexes,:]
                    preds_batch1 = preds_batch
                    preds_batch = running_mean(preds_batch1, preds_i, mi+1)
                    if return_var:
                        preds_batch_var = running_varsum(preds_batch_var, preds_i, preds_batch1, preds_batch)
                    if return_all:
                        preds_all[si:si+Nb,:,:,mi] = preds_i[:Nb,:,target_indexes]

                if rc_avg:
                    # construct reverse complement
                    fd[self.inputs] = hot1_rc(Xb)

                    for mi in range(mcr_n):
                        preds_i = sess.run(self.preds_op, feed_dict=fd)[:,::-1,:][:,ds_indexes,:]
                        preds_batch1 = preds_batch
                        preds_batch = running_mean(preds_batch1, preds_i, mcf_n+mi+1)
                        if return_var:
                            preds_batch_var = running_varsum(preds_batch_var, preds_i, preds_batch1, preds_batch)
                        if return_all:
                            preds_all[si:si+Nb,:,:,mcf_n+mi] = preds_i[:Nb,:,target_indexes]

            elif rc_avg:
                # compute reverse complement prediction
                fd[self.inputs] = hot1_rc(Xb)
                preds_batch_rc = sess.run(self.preds_op, feed_dict=fd)[:,::-1,:][:,ds_indexes,:]

                # average with forward prediction
                preds_batch += preds_batch_rc
                preds_batch /= 2.

            # filter for specific targets
            if target_indexes is not None:
                preds_batch = preds_batch[:,:,target_indexes]
                if mc_n > 0 and return_var:
                    preds_batch_var = preds_batch_var[:,:,target_indexes]

            # accumulate predictions
            preds[si:si+Nb,:,:] = preds_batch[:Nb,:,:]
            if mc_n > 0 and return_var:
                preds_var[si:si+Nb,:,:] = preds_batch_var[:Nb,:,:] / (mc_n-1)

            # update sequence index
            si += Nb

            # next batch
            Xb, _, _, Nb = batcher.next()

        # reset batcher
        batcher.reset()

        if return_var:
            if return_all:
                return preds, preds_var, preds_all
            else:
                return preds, preds_var
        else:
            return preds


    def predict_genes(self, sess, batcher, transcript_map, rc_avg=False, target_indexes=None):
        ''' Compute predictions on a test set.

        In
         sess: TensorFlow session
         batcher: Batcher class with transcript-covering sequences
         transcript_map: OrderedDict mapping transcript id's to (sequence index, position) tuples marking TSSs.
         rc_avg: Average predictions from the forward and reverse complement sequences
         target_indexes: Optional target subset list

        Out
         transcript_preds: G (gene transcripts) X T (targets) array
        '''

        # setup feed dict
        fd = self.set_mode('test')

        # initialize prediction arrays
        num_targets = self.num_targets
        if target_indexes is not None:
            num_targets = len(target_indexes)

        # initialize gene target predictions
        num_genes = len(transcript_map)
        gene_preds = np.zeros((num_genes, num_targets), dtype='float16')

        # construct an inverse map
        sequence_pos_transcripts = []
        txi = 0
        for transcript in transcript_map:
            si, pos = transcript_map[transcript]

            # extend sequence list
            while len(sequence_pos_transcripts) <= si:
                sequence_pos_transcripts.append({})

            # add gene to position set
            sequence_pos_transcripts[si].setdefault(pos,set()).add(txi)

            txi += 1

        '''
        sequence_transcripts = []
        txi = 0
        for transcript in transcript_map:
            tsi, tpos = transcript_map[transcript]
            while len(sequence_transcripts) <= tsi:
                sequence_transcripts.append([])
            sequence_transcripts[tsi].append((txi,tpos))
            txi += 1
        '''

        si = 0

        # get first batch
        Xb, _, _, Nb = batcher.next()

        while Xb is not None:
            # update feed dict
            fd[self.inputs] = Xb

            # compute predictions
            preds_batch = sess.run(self.preds_op, feed_dict=fd)

            if rc_avg:
                # compute reverse complement prediction
                fd[self.inputs] = hot1_rc(Xb)
                preds_batch_rc = sess.run(self.preds_op, feed_dict=fd)

                # average with forward prediction
                preds_batch += preds_batch_rc[:,::-1,:]
                preds_batch /= 2.

            # filter for specific targets
            if target_indexes is not None:
                preds_batch = preds_batch[:,:,target_indexes]

            # for each sequence in the batch
            for pi in range(Nb):
                '''
                # for each transcript in the sequence
                for txi, tpos in sequence_transcripts[si+pi]:
                    # adjust for the buffer
                    ppos = tpos - self.batch_buffer//self.target_pool

                    # save transcript prediction
                    transcript_preds[txi,:] = preds_batch[pi,ppos,:]
                '''

                for tpos in sequence_pos_transcripts[si+pi]:
                    for txi in sequence_pos_transcripts[si+pi][tpos]:
                        # adjust for the buffer
                        ppos = tpos - self.batch_buffer//self.target_pool

                        # add prediction
                        gene_preds[txi,:] += preds_batch[pi,ppos,:]

            # update sequence index
            si += Nb

            # next batch
            Xb, _, _, Nb = batcher.next()

        # reset batcher
        batcher.reset()

        return gene_preds


    def set_mode(self, mode):
        ''' Construct a feed dictionary to specify the model's mode. '''
        fd = {}

        if mode in ['train', 'training']:
            fd[self.is_training] = True
            for li in range(self.cnn_layers):
                fd[self.cnn_dropout_ph[li]] = self.cnn_dropout[li]
            for li in range(self.full_layers):
                fd[self.full_dropout_ph[li]] = self.full_dropout[li]

        elif mode in ['test', 'testing', 'evaluate']:
            fd[self.is_training] = False
            for li in range(self.cnn_layers):
                fd[self.cnn_dropout_ph[li]] = 0
            for li in range(self.full_layers):
                fd[self.full_dropout_ph[li]] = 0

        elif mode in ['test_mc', 'testing_mc', 'evaluate_mc', 'mc_test', 'mc_testing', 'mc_evaluate']:
            fd[self.is_training] = False
            for li in range(self.cnn_layers):
                fd[self.cnn_dropout_ph[li]] = self.cnn_dropout[li]
            for li in range(self.full_layers):
                fd[self.full_dropout_ph[li]] = self.full_dropout[li]

        else:
            print('Cannot recognize mode %s' % mode)
            exit(1)

        return fd


    def set_params(self, job):
        ''' Set model parameters. '''

        ###################################################
        # data attributes
        ###################################################
        self.seq_depth = job.get('seq_depth', 4)
        self.num_targets = job['num_targets']
        self.target_classes = job.get('target_classes', 1)
        self.target_pool = job.get('target_pool', 1)

        ###################################################
        # batching
        ###################################################
        self.batch_size = job.get('batch_size', 64)
        self.batch_length = job.get('batch_length', 1024)
        self.batch_buffer = job.get('batch_buffer', 64)

        ###################################################
        # training
        ###################################################
        self.learning_rate = job.get('learning_rate', 0.001)
        self.adam_beta1 = job.get('adam_beta1', 0.9)
        self.adam_beta2 = job.get('adam_beta2', 0.999)
        self.adam_eps = job.get('adam_eps', 1e-8)
        self.momentum = job.get('momentum', 0)
        self.decay = job.get('decay', 0.9)
        self.optimization = job.get('optimization', 'adam').lower()
        self.grad_clip = job.get('grad_clip', None)

        ###################################################
        # CNN params
        ###################################################
        self.cnn_filters = np.atleast_1d(job.get('cnn_filters', []))
        self.cnn_filter_sizes = np.atleast_1d(job.get('cnn_filter_sizes', []))
        self.cnn_layers = len(self.cnn_filters)

        self.cnn_pool = layer_extend(job.get('cnn_pool', []), 1, self.cnn_layers)
        self.cnn_strides = layer_extend(job.get('cnn_strides', []), 1, self.cnn_layers)
        self.cnn_dense = layer_extend(job.get('cnn_dense', []), False, self.cnn_layers)
        self.cnn_dilation = layer_extend(job.get('cnn_dilation', []), 1, self.cnn_layers)

        ###################################################
        # fully connected params
        ###################################################
        self.full_units = np.atleast_1d(job.get('full_units', []))
        self.full_layers = len(self.full_units)

        ###################################################
        # regularization
        ###################################################
        self.cnn_dropout = layer_extend(job.get('cnn_dropout', []), 0, self.cnn_layers)
        self.full_dropout = layer_extend(job.get('full_dropout', []), 0, self.full_layers)

        self.cnn_l2 = layer_extend(job.get('cnn_l2', []), 0, self.cnn_layers)
        self.full_l2 = layer_extend(job.get('full_l2', []), 0, self.full_layers)

        self.final_l1 = job.get('final_l1', 0)

        self.batch_renorm = bool(job.get('batch_renorm', False))
        self.batch_renorm = bool(job.get('renorm', self.batch_renorm))

        ###################################################
        # loss
        ###################################################
        self.link = job.get('link', 'exp_linear')
        self.loss = job.get('loss', 'poisson')


        ###################################################
        # other
        ###################################################
        self.save_reprs = job.get('save_reprs', False)


    def test(self, sess, batcher, rc_avg=True, mc_n=0, down_sample=1):
        ''' Compute model accuracy on a test set.

        Args:
          sess:         TensorFlow session
          batcher:      Batcher object to provide data
          rc_avg:       Perform half the iterations on the rc seq and average.
          mc_n:         Monte Carlo iterations
          down_sample:  Int specifying to consider uniformly spaced sampled positions

        Returns:
          acc:          Accuracy object
        '''

        batch_losses = []
        batch_target_losses = []

        # determine non-buffer region
        buf_start = self.batch_buffer // self.target_pool
        buf_end = (self.batch_length - self.batch_buffer) // self.target_pool
        buf_len = buf_end - buf_start

        # uniformly sample indexes
        ds_indexes = np.arange(0, buf_len, down_sample)

        # initialize prediction and target arrays
        preds = np.zeros((batcher.num_seqs, len(ds_indexes), self.num_targets), dtype='float16')
        targets = np.zeros((batcher.num_seqs, len(ds_indexes), self.num_targets), dtype='float16')
        targets_na = np.zeros((batcher.num_seqs, len(ds_indexes)), dtype='bool')
        si = 0

        if mc_n > 0:
            # setup feed dict
            fd = self.set_mode('test_mc')

            # divide iterations between forward and reverse
            mcf_n = mc_n
            if rc_avg:
                mcr_n = mc_n // 2
                mcf_n = mc_n - mcr_n
        else:
            # setup feed dict
            fd = self.set_mode('test')

        # get first batch
        Xb, Yb, NAb, Nb = batcher.next()

        while Xb is not None:
            # update feed dict
            fd[self.inputs] = Xb
            fd[self.targets] = Yb
            fd[self.targets_na] = NAb

            # initialize accumulator
            preds_batch, targets_batch, loss_batch, target_losses_batch = sess.run([self.preds_op, self.targets_op, self.loss_op, self.target_losses], feed_dict=fd)

            if mc_n > 0:
                # accumulate predictions
                for mi in range(1,mcf_n):
                    preds_batch += sess.run(self.preds_op, feed_dict=fd)

                if rc_avg:
                    # construct reverse complement
                    fd[self.inputs] = hot1_rc(Xb)

                    # initialize accumulator
                    preds_batch_rc = sess.run(self.preds_op, feed_dict=fd)

                    # accumulate predictions
                    for mi in range(1,mcr_n):
                        preds_batch_rc += sess.run(self.preds_op, feed_dict=fd)

                    # sum with forward predictions
                    preds_batch += preds_batch_rc[:,::-1,:]

                # average all predictions
                preds_batch /= mc_n

                # recompute loss
                fd[self.preds_adhoc] = preds_batch
                loss_batch, target_losses_batch = sess.run([self.loss_adhoc, self.target_losses_adhoc], feed_dict=fd)

            elif rc_avg:
                # compute reverse complement prediction
                fd[self.inputs] = hot1_rc(Xb)
                preds_batch_rc = sess.run(self.preds_op, feed_dict=fd)

                # average with forward prediction
                preds_batch += preds_batch_rc[:,::-1,:]
                preds_batch /= 2.

                # recompute loss
                fd[self.preds_adhoc] = preds_batch
                loss_batch, target_losses_batch = sess.run([self.loss_adhoc, self.target_losses_adhoc], feed_dict=fd)

            # accumulate predictions and targets
            if preds_batch.ndim == 3:
                preds[si:si+Nb,:,:] = preds_batch[:Nb,ds_indexes,:]
                targets[si:si+Nb,:,:] = targets_batch[:Nb,ds_indexes,:]
            else:
                for qi in range(preds_batch.shape[3]):
                    # TEMP, ideally this will be in the HDF5 and set previously
                    self.quantile_means = np.geomspace(0.1, 256, 16)

                    # softmax
                    preds_batch_norm = np.expand_dims(np.sum(np.exp(preds_batch[:Nb,ds_indexes,:,:]),axis=3),axis=3)
                    pred_probs_batch = np.exp(preds_batch[:Nb,ds_indexes,:,:]) / preds_batch_norm

                    # expectation over quantile medians
                    preds[si:si+Nb,:,:] = np.dot(pred_probs_batch, self.quantile_means)

                    # compare to quantile median
                    targets[si:si+Nb,:,:] = self.quantile_means[targets_batch[:Nb,ds_indexes,:]-1]

            # accumulate loss
            batch_losses.append(loss_batch)
            batch_target_losses.append(target_losses_batch)

            # update sequence index
            si += Nb

            # next batch
            Xb, Yb, NAb, Nb = batcher.next()

        # reset batcher
        batcher.reset()

        # mean across batches
        batch_losses = np.mean(batch_losses)
        batch_target_losses = np.array(batch_target_losses).mean(axis=0)

        # instantiate accuracy object
        acc = basenji.accuracy.Accuracy(targets, preds, targets_na, batch_losses, batch_target_losses)

        return acc


    def train_epoch(self, sess, batcher, rc=False, sum_writer=None):
        ''' Execute one training epoch '''

        # initialize training loss
        train_loss = []

        # setup feed dict
        fd = self.set_mode('train')

        # get first batch
        Xb, Yb, NAb, Nb = batcher.next(rc)

        while Xb is not None and Nb == self.batch_size:
            # update feed dict
            fd[self.inputs] = Xb
            fd[self.targets] = Yb
            fd[self.targets_na] = NAb

            run_returns = sess.run([self.merged_summary, self.loss_op, self.step_op]+self.update_ops, feed_dict=fd)
            summary, loss_batch = run_returns[:2]

            # pull gradients
            # gvs_batch = sess.run([g for (g,v) in self.gvs if g is not None], feed_dict=fd)

            # add summary
            if sum_writer is not None:
                sum_writer.add_summary(summary, self.step)

            # accumulate loss
            # avail_sum = np.logical_not(NAb[:Nb,:]).sum()
            # train_loss.append(loss_batch / avail_sum)
            train_loss.append(loss_batch)

            # next batch
            Xb, Yb, NAb, Nb = batcher.next(rc)
            self.step += 1

        # reset training batcher
        batcher.reset()

        return np.mean(train_loss)


def layer_extend(var, default, layers):
    ''' Process job input to extend for the
         proper number of layers. '''

    # if it's a number
    if type(var) != list:
        # change the default to that number
        default = var

        # make it a list
        var = [var]

    # extend for each layer
    while len(var) < layers:
        var.append(default)

    return var

def running_mean(u_k1, x_k, k):
    return u_k1 + (x_k - u_k1) / k

def running_varsum(v_k1, x_k, m_k1, m_k):
    ''' Computing the running variance numerator.

    Ref: https://www.johndcook.com/blog/standard_deviation/
    '''
    return v_k1 + (x_k - m_k1)*(x_k - m_k)