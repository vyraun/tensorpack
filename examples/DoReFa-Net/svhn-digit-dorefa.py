#!/usr/bin/env python
# -*- coding: UTF-8 -*-
# File: svhn-digit-dorefa.py
# Author: Yuxin Wu <ppwwyyxx@gmail.com>

import tensorflow as tf
import argparse
import numpy as np
import os

from tensorpack import *
from tensorpack.tfutils.symbolic_functions import *
from tensorpack.tfutils.summary import *

"""
Code for the paper:
DoReFa-Net: Training Low Bitwidth Convolutional Neural Networks with Low Bitwidth Gradients
http://arxiv.org/abs/1606.06160

The original experiements are performed on a proprietary framework.
This is our attempt to reproduce it on tensorpack.

You'll need tcmalloc to avoid large memory consumption: https://github.com/tensorflow/tensorflow/issues/2942

This config, with (W,A,G)=(1,1,4), can reach 3.1~3.2% error after 150 epochs.
With the GaussianDeform augmentor, it will reach 2.8~2.9%
(we are not using this augmentor in the paper).
"""

BITW = 1
BITA = 2
BITG = 4

GRAD_DEFINED = False
def get_dorefa(bitW, bitA, bitG):
    """ return the three quantization functions fw, fa, fg, for weights,
    activations and gradients respectively"""
    G = tf.get_default_graph()

    def quantize(x, k):
        n = float(2**k-1)
        with G.gradient_override_map({"Floor": "Identity"}):
            return tf.round(x * n) / n

    def fw(x):
        if bitW == 32:
            return x
        if bitW == 1:   # BWN
            with G.gradient_override_map({"Sign": "Identity"}):
                E = tf.stop_gradient(tf.reduce_mean(tf.abs(x)))
                return tf.sign(x / E) * E
        x = tf.tanh(x)
        x = x / tf.reduce_max(tf.abs(x)) * 0.5 + 0.5
        return 2 * quantize(x, bitW) - 1

    def fa(x):
        if bitA == 32:
            return x
        return quantize(x, bitA)

    global GRAD_DEFINED
    if not GRAD_DEFINED:
        @tf.RegisterGradient("FGGrad")
        def grad_fg(op, x):
            rank = x.get_shape().ndims
            assert rank is not None
            maxx = tf.reduce_max(tf.abs(x), list(range(1,rank)), keep_dims=True)
            x = x / maxx
            n = float(2**bitG-1)
            x = x * 0.5 + 0.5 + tf.random_uniform(
                    tf.shape(x), minval=-0.5/n, maxval=0.5/n)
            x = tf.clip_by_value(x, 0.0, 1.0)
            x = quantize(x, bitG) - 0.5
            return x * maxx * 2
    GRAD_DEFINED = True

    def fg(x):
        if bitG == 32:
            return x
        with G.gradient_override_map({"Identity": "FGGrad"}):
            return tf.identity(x)
    return fw, fa, fg

class Model(ModelDesc):
    def _get_input_vars(self):
        return [InputVar(tf.float32, [None, 40, 40, 3], 'input'),
                InputVar(tf.int32, [None], 'label') ]

    def _build_graph(self, input_vars, is_training):
        image, label = input_vars

        fw, fa, fg = get_dorefa(BITW, BITA, BITG)
        # monkey-patch tf.get_variable to apply fw
        old_get_variable = tf.get_variable
        def new_get_variable(name, shape=None, **kwargs):
            v = old_get_variable(name, shape, **kwargs)
            # don't binarize first and last layer
            if name != 'W' or 'conv0' in v.op.name or 'fc' in v.op.name:
                return v
            else:
                logger.info("Binarizing weight {}".format(v.op.name))
                return fw(v)
        tf.get_variable = new_get_variable


        def cabs(x):
            return tf.minimum(1.0, tf.abs(x), name='cabs')
        def activate(x):
            return fa(cabs(x))

        image = image / 256.0

        with argscope(BatchNorm, decay=0.9, epsilon=1e-4, use_local_stat=is_training), \
                argscope(Conv2D, use_bias=False, nl=tf.identity):
            logits = (LinearWrap(image)
                .Conv2D('conv0', 48, 5, padding='VALID', use_bias=True)
                .MaxPooling('pool0', 2, padding='SAME')
                .apply(activate)
                # 18
                .Conv2D('conv1', 64, 3, padding='SAME')
                .apply(fg)
                .BatchNorm('bn1').apply(activate)

                .Conv2D('conv2', 64, 3, padding='SAME')
                .apply(fg)
                .BatchNorm('bn2')
                .MaxPooling('pool1', 2, padding='SAME')
                .apply(activate)
                # 9
                .Conv2D('conv3', 128, 3, padding='VALID')
                .apply(fg)
                .BatchNorm('bn3').apply(activate)
                # 7

                .Conv2D('conv4', 128, 3, padding='SAME')
                .apply(fg)
                .BatchNorm('bn4').apply(activate)

                .Conv2D('conv5', 128, 3, padding='VALID')
                .apply(fg)
                .BatchNorm('bn5').apply(activate)
                # 5
                .tf.nn.dropout(0.5 if is_training else 1.0)
                .Conv2D('conv6', 512, 5, padding='VALID')
                .apply(fg).BatchNorm('bn6')
                .apply(cabs)
                .FullyConnected('fc1', 10, nl=tf.identity)())
        tf.get_variable = old_get_variable
        prob = tf.nn.softmax(logits, name='output')

        cost = tf.nn.sparse_softmax_cross_entropy_with_logits(logits, label)
        cost = tf.reduce_mean(cost, name='cross_entropy_loss')
        tf.add_to_collection(MOVING_SUMMARY_VARS_KEY, cost)

        # compute the number of failed samples
        wrong = prediction_incorrect(logits, label)
        nr_wrong = tf.reduce_sum(wrong, name='wrong')
        # monitor training error
        tf.add_to_collection(MOVING_SUMMARY_VARS_KEY,
                tf.reduce_mean(wrong, name='train_error'))

        # weight decay on all W of fc layers
        wd_cost = regularize_cost('fc.*/W', l2_regularizer(1e-7))
        tf.add_to_collection(MOVING_SUMMARY_VARS_KEY, wd_cost)

        add_param_summary([('.*/W', ['histogram', 'rms'])])
        self.cost = tf.add_n([cost, wd_cost], name='cost')

def get_config():
    logger.auto_set_dir()

    # prepare dataset
    d1 = dataset.SVHNDigit('train')
    d2 = dataset.SVHNDigit('extra')
    data_train = RandomMixData([d1, d2])
    data_test = dataset.SVHNDigit('test')

    augmentors = [
        imgaug.Resize((40, 40)),
        imgaug.Brightness(30),
        imgaug.Contrast((0.5,1.5)),
        #imgaug.GaussianDeform(  # this is slow but helpful. only use it when you have lots of cpus
            #[(0.2, 0.2), (0.2, 0.8), (0.8,0.8), (0.8,0.2)],
            #(40,40), 0.2, 3),
    ]
    data_train = AugmentImageComponent(data_train, augmentors)
    data_train = BatchData(data_train, 128)
    data_train = PrefetchDataZMQ(data_train, 5)
    step_per_epoch = data_train.size()

    augmentors = [ imgaug.Resize((40, 40)) ]
    data_test = AugmentImageComponent(data_test, augmentors)
    data_test = BatchData(data_test, 128, remainder=True)

    lr = tf.train.exponential_decay(
        learning_rate=1e-3,
        global_step=get_global_step_var(),
        decay_steps=data_train.size() * 100,
        decay_rate=0.5, staircase=True, name='learning_rate')
    tf.scalar_summary('learning_rate', lr)

    return TrainConfig(
        dataset=data_train,
        optimizer=tf.train.AdamOptimizer(lr, epsilon=1e-5),
        callbacks=Callbacks([
            StatPrinter(),
            ModelSaver(),
            InferenceRunner(data_test,
                [ScalarStats('cost'), ClassificationError()])
        ]),
        model=Model(),
        step_per_epoch=step_per_epoch,
        max_epoch=200,
    )

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='the GPU to use') # nargs='*' in multi mode
    parser.add_argument('--load', help='load a checkpoint')
    parser.add_argument('--dorefa',
            help='number of bits for W,A,G, separated by comma. Defaults to \'1,2,4\'',
            default='1,2,4')
    args = parser.parse_args()

    BITW, BITA, BITG = map(int, args.dorefa.split(','))

    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

    config = get_config()
    if args.load:
        config.session_init = SaverRestore(args.load)
    if args.gpu:
        config.nr_tower = len(args.gpu.split(','))
    QueueInputTrainer(config).train()
