# Copyright (c) 2021 Graphcore Ltd. All rights reserved.

import unittest
from pathlib import Path
import sys
from tensorflow import keras
import tensorflow as tf
import numpy as np
sys.path.append(str(Path(__file__).absolute().parent.parent))
from model.model_editor import ModelEditor


class CopyModelWeightsTest(unittest.TestCase):

    def test_copy_model_with_weights(self):
        # example usage
        initial_model = keras.applications.resnet50.ResNet50(
            weights='imagenet', input_shape=(224, 224, 3), classes=1000)
        model_editor = ModelEditor(initial_model)
        copied_model = model_editor.update_model_with_func(user_fn=lambda current_layer, layer_inputs: None)

        # test
        np.random.seed(0)
        image_0 = np.zeros((1, 224, 224, 3))
        image_1 = np.random.random((1, 224, 224, 3)) * 10
        assert((initial_model.predict(image_0) == copied_model.predict(image_0)).all())
        assert((initial_model.predict(image_1) == copied_model.predict(image_1)).all())

    # test for copy weights when some of the layers has changed
    def test_copy_not_all_weights(self):
        # example usage
        def add_dummy_layers_fn(current_layer, layer_input):
            if isinstance(current_layer, keras.layers.InputLayer):
                layer_output = DummyLayer()(layer_input)
                return layer_output

            if current_layer.name == model_editor.model.layers[-1].name:
                layer_output = current_layer(layer_input)
                layer_output = DummyLayer()(layer_output)
                return layer_output

        initial_model = keras.applications.resnet50.ResNet50(
            weights='imagenet', input_shape=(224, 224, 3), classes=1000)
        model_editor = ModelEditor(initial_model)
        copied_model = model_editor.update_model_with_func(user_fn=add_dummy_layers_fn)

        # test
        np.random.seed(0)
        image_0 = np.zeros((1, 224, 224, 3))
        image_1 = np.random.random((1, 224, 224, 3)) * 10
        assert(len(copied_model.layers) == len(initial_model.layers) + 2)
        assert((initial_model.predict(image_0) == copied_model.predict(image_0)).all())
        assert((initial_model.predict(image_1) == copied_model.predict(image_1)).all())


class ReplaceInputTest(unittest.TestCase):

    def test_preappend_layers(self):
        # example usage
        def preappend_layers_fn(current_layer, sub_input):
            if isinstance(current_layer, keras.layers.InputLayer):
                sub_output = keras.layers.MaxPooling2D(name='added1')(sub_input)
                sub_output_1 = keras.layers.Conv2D(filters=32, kernel_size=3, name='added2')(sub_output)
                sub_output_2 = keras.layers.Conv2D(filters=32, kernel_size=3, name='added3')(sub_output)
                sub_output = keras.layers.Add(name='added4')([sub_output_1, sub_output_2])
                return sub_output

        initial_model = initial_model_1()
        model_editor = ModelEditor(initial_model)
        modified_model = model_editor.update_model_with_func(preappend_layers_fn, copy_weights=False)

        # test
        expected_model = expected_model_1()
        assert_same_config(self, modified_model, expected_model)


class ReplaceMiddleLayersTest(unittest.TestCase):

    def test_replace_layers_by_name(self):
        # example usage
        def middle_layers_fn(current_layer, sub_input):
            if isinstance(current_layer, keras.layers.MaxPooling2D):
                sub_output = keras.layers.BatchNormalization()(sub_input)
                return sub_output
            if isinstance(current_layer, keras.layers.Flatten):
                sub_output = keras.layers.MaxPooling2D()(sub_input)
                sub_output = keras.layers.Flatten()(sub_output)
                return sub_output

        initial_model = initial_model_1()
        model_editor = ModelEditor(initial_model)
        modified_model = model_editor.update_model_with_func(middle_layers_fn)

        # test
        expected_model = expected_model_2()
        assert_same_config(self, modified_model, expected_model)


class ReplaceLastLayerTest(unittest.TestCase):

    def test_append_layer(self):
        # example usage
        def append_layer_fn(current_layer, sub_input):
            if current_layer.name == model_editor.model.layers[-1].name:
                sub_output = current_layer(sub_input)
                sub_output = keras.layers.Dense(10)(sub_output)
                return sub_output

        initial_model = initial_model_1()
        model_editor = ModelEditor(initial_model)
        modified_model = model_editor.update_model_with_func(append_layer_fn)

        # test
        expected_model = expected_model_3()
        assert_same_config(self, modified_model, expected_model)


class EdgeCasesTest(unittest.TestCase):

    def test_layer_with_two_outputs(self):
        # example usage
        initial_model = initial_model_2()
        model_editor = ModelEditor(initial_model)
        modified_model = model_editor.update_model_with_func(user_fn=lambda current_layer, layer_inputs: None)

        # test
        expected_model = initial_model
        assert_same_config(self, modified_model, expected_model)

    def test_layer_multi_outputs_inputs(self):
        # example usage
        initial_model = initial_model_3()
        model_editor = ModelEditor(initial_model)
        modified_model = model_editor.update_model_with_func(user_fn=lambda current_layer, layer_inputs: None)

        # test
        expected_model = initial_model
        assert_same_config(self, modified_model, expected_model)


# assert that model have the same configuration
def assert_same_config(self, modified_model, expected_model):
    self.assertEqual(len(modified_model.layers), len(expected_model.layers),
                     'number of layers in modified model is not the same as in expected model')
    for modified_layer, expected_layer in zip(modified_model.layers, expected_model.layers):
        modified_config, expected_config = modified_layer.get_config(), expected_layer.get_config()
        modified_config.pop('name'), expected_config.pop('name')
        self.assertEqual(modified_config, expected_config,
                         f'failed on the following layers: {modified_layer.name} != {expected_layer.name}')


# custom Layers for tests
class DummyLayer(keras.layers.Layer):
    def __init__(self, **kwargs):
        super(DummyLayer, self).__init__()

    def call(self, input_tensor):
        return input_tensor


class MyTwoOutputLayer(keras.layers.Layer):
    def __init__(self, **kwargs):
        super(MyTwoOutputLayer, self).__init__()
        self.conv2a = keras.layers.Conv2D(32, (1, 1))
        self.bn2a = keras.layers.BatchNormalization()

    def call(self, input_tensor):
        x1 = self.conv2a(input_tensor)
        x2 = self.bn2a(x1)
        return tf.nn.relu(x1), tf.nn.relu(x2)


# Toy examples to test ModelManipulator (all expected models are editied initial_model_1)

def initial_model_1():
    model_input = keras.Input(shape=(32, 32, 3))
    model_output = keras.layers.MaxPooling2D()(model_input)
    model_output_1 = keras.layers.Conv2D(filters=32, kernel_size=3)(model_output)
    model_output_2 = keras.layers.Conv2D(filters=32, kernel_size=3)(model_output)
    model_output = keras.layers.Add()([model_output_1, model_output_2])
    model_output = keras.layers.Flatten()(model_output)
    return keras.Model(model_input, model_output)


def initial_model_2():
    model_input = keras.Input(shape=(32, 32, 3))
    x1, x2 = MyTwoOutputLayer()(model_input)
    x1 = keras.layers.Conv2D(filters=32, kernel_size=3)(x1)
    x2 = keras.layers.Conv2D(filters=32, kernel_size=3)(x2)
    x = keras.layers.Add()([x1, x2])
    return keras.Model(model_input, x)


def initial_model_3():
    model_input_1 = keras.Input(shape=(32, 32, 3))
    model_input_2 = keras.Input(shape=(32, 32, 3))
    x1 = keras.layers.Conv2D(filters=32, kernel_size=3)(model_input_1)
    x2 = keras.layers.Conv2D(filters=32, kernel_size=3)(model_input_2)
    x = keras.layers.Add()([x1, x2])
    x1 = keras.layers.Conv2D(filters=32, kernel_size=3)(x)
    x2 = keras.layers.Conv2D(filters=32, kernel_size=3)(x)
    return keras.Model(inputs=[model_input_1, model_input_2], outputs=[x1, x2])


def expected_model_1():
    model_input = keras.Input(shape=(32, 32, 3))
    model_output = keras.layers.MaxPooling2D()(model_input)
    model_output_1 = keras.layers.Conv2D(filters=32, kernel_size=3)(model_output)
    model_output_2 = keras.layers.Conv2D(filters=32, kernel_size=3)(model_output)
    model_output = keras.layers.Add()([model_output_1, model_output_2])
    model_output = keras.layers.MaxPooling2D()(model_output)
    model_output_1 = keras.layers.Conv2D(filters=32, kernel_size=3)(model_output)
    model_output_2 = keras.layers.Conv2D(filters=32, kernel_size=3)(model_output)
    model_output = keras.layers.Add()([model_output_1, model_output_2])
    model_output = keras.layers.Flatten()(model_output)
    return keras.Model(model_input, model_output)


def expected_model_2():
    model_input = keras.Input(shape=(32, 32, 3))
    model_output = keras.layers.BatchNormalization()(model_input)
    model_output_1 = keras.layers.Conv2D(filters=32, kernel_size=3)(model_output)
    model_output_2 = keras.layers.Conv2D(filters=32, kernel_size=3)(model_output)
    model_output = keras.layers.Add()([model_output_1, model_output_2])
    model_output = keras.layers.MaxPooling2D()(model_output)
    model_output = keras.layers.Flatten()(model_output)
    return keras.Model(model_input, model_output)


def expected_model_3():
    model_input = keras.Input(shape=(32, 32, 3))
    model_output = keras.layers.MaxPooling2D()(model_input)
    model_output_1 = keras.layers.Conv2D(filters=32, kernel_size=3)(model_output)
    model_output_2 = keras.layers.Conv2D(filters=32, kernel_size=3)(model_output)
    model_output = keras.layers.Add()([model_output_1, model_output_2])
    model_output = keras.layers.Flatten()(model_output)
    model_output = keras.layers.Dense(10)(model_output)
    return keras.Model(model_input, model_output)
