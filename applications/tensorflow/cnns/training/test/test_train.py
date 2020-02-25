# Copyright 2019 Graphcore Ltd.
"""
Tests covering various CNN training options.
"""

import unittest
import statistics
from test_common import get_csv, run_train, cifar10_data_dir


class TestBasicFunctionality(unittest.TestCase):

    def test_help(self):
        help_out = run_train(**{'--help': ''})
        self.assertNotEqual(help_out.find("usage: train.py"), -1)


class TestMisc(unittest.TestCase):
    """Some miscellaneous options"""

    @classmethod
    def setUpClass(cls):
        out = run_train(**{'--data-dir': cifar10_data_dir,
                           '--name-suffix': 'penguin',
                           '--log-dir': 'logs/walrus',
                           '--epochs': 8})
        cls.logdir = None
        for line in out.split('\n'):
            if line.find('Saving to ') != -1:
                cls.logdir = line[11:]
                break
        cls.validation = get_csv(out, 'validation.csv')
        cls.training = get_csv(out, 'training.csv')

    def test_logdir(self):
        self.assertEqual(self.logdir[:12], 'logs/walrus/')

    def test_name_suffix(self):
        self.assertNotEqual(self.logdir.find('penguin'), -1)


class TestCifar10Training(unittest.TestCase):
    """Testing some basic training parameters"""

    @classmethod
    def setUpClass(cls):
        out = run_train(**{'--data-dir': cifar10_data_dir,
                           '--epochs': 10,
                           '--warmup-epochs': 0,
                           '--learning-rate-decay': '0.1',
                           '--learning-rate-schedule': '0.5,0.75,0.875'})
        cls.validation = get_csv(out, 'validation.csv')
        cls.training = get_csv(out, 'training.csv')

    def test_final_validation_accuracy(self):
        final_acc = self.validation['val_acc'][-1]
        self.assertGreater(final_acc, 82)
        self.assertLess(final_acc, 87)

    def test_final_training_accuracy(self):
        final_acc = self.training['train_acc_avg'][-1]
        self.assertGreater(final_acc, 82)
        self.assertLess(final_acc, 87)

    def test_learning_rates(self):
        self.assertEqual(self.training['lr'][0], 0.5)
        self.assertEqual(self.training['lr'][-1], 0.0005)

    def test_epochs_completed(self):
        self.assertEqual(round(self.training['epoch'][-1]), 10)


class TestCifar10FullTraining(unittest.TestCase):
    """Fast training of Cifar-10 to good accuracy"""

    @classmethod
    def setUpClass(cls):
        out = run_train(**{'--data-dir': cifar10_data_dir,
                           '--epochs': 50,
                           '--batch-size': 48,
                           '--warmup-epochs': 2,
                           '--lr-schedule': 'cosine',
                           '--label-smoothing': '0.05',
                           '--base-learning-rate': -5,
                           '--precision': '16.32'})
        cls.validation = get_csv(out, 'validation.csv')
        cls.training = get_csv(out, 'training.csv')

    def test_final_validation_accuracy(self):
        final_acc = statistics.median(self.validation['val_acc'][-3:-1])
        self.assertGreater(final_acc, 91.0)
        self.assertLess(final_acc, 93.0)

    def test_final_training_accuracy(self):
        final_acc = self.training['train_acc_avg'][-1]
        self.assertGreater(final_acc, 96)
        self.assertLess(final_acc, 98)

    def test_final_loss(self):
        self.assertLess(self.training['loss_batch'][-1], 0.45)
        self.assertGreater(self.training['loss_batch'][-1], 0.35)

    def test_epochs_completed(self):
        self.assertEqual(round(self.training['epoch'][-1]), 50)


class TestResNet50SingleIPUTraining(unittest.TestCase):
    """ResNet-50 example on a single IPU"""

    @classmethod
    def setUpClass(cls):
        out = run_train(**{'--synthetic-data': '',
                           '--dataset': 'ImageNet',
                           '--model-size': 50,
                           '--batch-size': 1,
                           '--available-memory-proportion': 0.1,
                           '--iterations': '2000'})
        cls.validation = get_csv(out, 'validation.csv')
        cls.training = get_csv(out, 'training.csv')

    def test_iterations_completed(self):
        self.assertEqual(self.training['iteration'][-1], 2000)


class TestShardedTraining(unittest.TestCase):
    """Example of sharding for model parallelism"""

    @classmethod
    def setUpClass(cls):
        out = run_train(**{'--synthetic-data': '',
                           '--dataset': 'ImageNet',
                           '--shards': 2,
                           '--iterations': '5000',
                           '--no-validation': ''})
        cls.training = get_csv(out, 'training.csv')

    def test_iterations_completed(self):
        self.assertEqual(self.training['iteration'][-1], 5000)


class TestPipelineResNet18(unittest.TestCase):
    """A simple pipelined model"""

    @classmethod
    def setUpClass(cls):
        out = run_train(**{'--iterations': 1000,
                           '--dataset': 'imagenet',
                           '--synthetic-data': '',
                           '--shards': 2,
                           '--pipeline-depth': 128,
                           '--batch-size': 1,
                           '--no-validation': '',
                           '--pipeline-splits': 'b2/0/relu'})
        cls.training = get_csv(out, 'training.csv')
        print(cls.training)

    def test_iterations_completed(self):
        self.assertEqual(self.training['iteration'][-1], 1000)
        self.assertEqual(self.training['step'][-1], 2)


class TestReplicatedTraining(unittest.TestCase):
    """Using replicas for data parallelism"""

    @classmethod
    def setUpClass(cls):
        out = run_train(**{'--data-dir': cifar10_data_dir,
                           '--model': 'resnet',
                           '--lr-schedule': 'stepped',
                           '--learning-rate-decay': 0.5,
                           '--learning-rate-schedule': '0.5,0.9',
                           '--epochs': 20,
                           '--replicas': 2})
        cls.validation = get_csv(out, 'validation.csv')
        cls.training = get_csv(out, 'training.csv')

    def test_final_training_accuracy(self):
        final_acc = self.training['train_acc_avg'][-1]
        self.assertGreater(final_acc, 85)
        self.assertLess(final_acc, 95)

    def test_epochs_completed(self):
        self.assertEqual(round(self.training['epoch'][-1]), 20)


class TestLotsOfOptions(unittest.TestCase):
    """Testing lots of other options to check they are still available"""

    @classmethod
    def setUpClass(cls):
        out = run_train(**{'--dataset': 'cifar-10',
                           '--epochs': 10,
                           '--model-size': 14,
                           '--batch-norm': '',
                           '--pipeline-num-parallel': 8,
                           '--synthetic-data': '',
                           '--batch-size': 16,
                           '--base-learning-rate': -4,
                           '--precision': '32.32',
                           '--seed': 1234,
                           '--warmup-epochs': 0,
                           '--no-stochastic-rounding': '',
                           '--batches-per-step': 100
                           })
        cls.validation = get_csv(out, 'validation.csv')
        cls.training = get_csv(out, 'training.csv')

    # We're mostly just testing that training still runs with all the above options.

    def test_learning_rate(self):
        self.assertEqual(self.training['lr'][0], 1.0)

    def test_epoch(self):
        self.assertEqual(int(self.validation['epoch'][-1] + 0.5), 10)