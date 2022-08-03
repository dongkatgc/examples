# Copyright (c) 2021 Graphcore Ltd. All rights reserved.

import os
import glob
import shutil
import logging
from time import time
from datetime import datetime

import tensorflow as tf
from tensorflow.python import ipu
from tensorflow.python.ipu import horovod as hvd
from tensorflow.python.ipu.ops import pipelining_ops
from tensorflow.python.ipu.horovod.popdist_strategy import PopDistStrategy
import popdist
import popdist.tensorflow

import precision
import time_to_train
from batch_config import BatchConfig
from ipu_config import configure_ipu
from callbacks.callback_factory import CallbackFactory
from batch_config import calculate_micro_batch_periodicity
from configuration import terminal_argparse
from datasets.dataset_factory import DatasetFactory
from eight_bit_transfer import EightBitTransfer
from losses.loss_enqueuer import (wrap_loss_in_allreduce_enqueuer,
                                  wrap_loss_in_enqueuer,
                                  wrap_loss_in_label_enqueuer,
                                  wrap_loss_in_pred_enqueuer)
from losses.smoothed_categorical_crossentropy import SmoothedCategoricalCrossentropy
from metrics.metric_enqueuer import (wrap_metric_in_allreduce_enqueuer,
                                     wrap_metric_in_enqueuer)
from model.model_factory import ModelFactory, replace_preprocess_layer_with_fn
from optimizers.optimizer_factory import OptimizerFactory
from schedules.scheduler_factory import get_lr_scheduler
import seed


if __name__ == '__main__':
    # configure logger
    logging.basicConfig(level=logging.INFO)

    hparams = terminal_argparse.handle_cmdline_arguments()
    hparams.seed = seed.set_host_seed(hparams.seed, hparams.deterministic)

    batch_config = BatchConfig(hparams.micro_batch_size,
                               hparams.num_replicas,
                               hparams.gradient_accumulation_count,
                               hparams.global_batch_size)

    hparams.gradient_accumulation_count = batch_config.gradient_accumulation_count
    hparams.global_batch_size = batch_config.global_batch_size

    if hparams.validation:

        if hparams.pipeline_validation_model:
            hparams.validation_num_replicas = hparams.validation_num_replicas or hparams.num_replicas
            validation_gradient_accumulation_count = 2 * (len(hparams.pipeline_splits) + 1)
            hparams.validation_ipus_per_replica = hparams.num_ipus_per_replica
        else:
            hparams.validation_num_replicas = hparams.validation_num_replicas or (
                hparams.num_replicas * hparams.num_ipus_per_replica)
            validation_gradient_accumulation_count = 1
            hparams.validation_ipus_per_replica = 1

        validation_batch_config = BatchConfig(micro_batch_size=hparams.validation_micro_batch_size,
                                              num_replicas=hparams.validation_num_replicas,
                                              gradient_accumulation_count=validation_gradient_accumulation_count,
                                              global_batch_size=None)

    fp_precision = precision.Precision(hparams.precision)
    fp_precision.apply()

    # get eight bit transfer object
    eight_bit_transfer = EightBitTransfer(fp_precision.compute_precision) if hparams.eight_bit_transfer else None

    hparams.num_local_instances = hvd.local_size() if hparams.distributed_training else 1

    # Get the training dataset
    train_app_dataset, accelerator_side_preprocess_train_fn, hparams.pipeline_num_parallel = DatasetFactory.get_dataset(
        dataset_name=hparams.dataset,
        dataset_path=hparams.dataset_path,
        split='train',
        img_datatype=fp_precision.compute_precision,
        batch_config=batch_config,
        seed=hparams.seed,
        shuffle=hparams.shuffle,
        deterministic=hparams.deterministic,
        accelerator_side_preprocess=hparams.accelerator_side_preprocess,
        eight_bit_transfer=eight_bit_transfer,
        pipeline_num_parallel=hparams.pipeline_num_parallel,
        num_local_instances=hparams.num_local_instances,
        fused_preprocessing=hparams.fused_preprocessing,
        synthetic_data=hparams.synthetic_data)
    logging.debug(train_app_dataset.pipeline)

    # Get the validation dataset
    if hparams.validation:
        validation_app_dataset, accelerator_side_preprocess_inference_fn, hparams.pipeline_num_parallel = DatasetFactory.get_dataset(
            dataset_name=hparams.dataset,
            dataset_path=hparams.dataset_path,
            split='test',
            img_datatype=fp_precision.compute_precision,
            batch_config=batch_config,
            seed=hparams.seed,
            accelerator_side_preprocess=hparams.accelerator_side_preprocess,
            eight_bit_transfer=eight_bit_transfer,
            pipeline_num_parallel=hparams.pipeline_num_parallel,
            num_local_instances=hparams.num_local_instances,
            fused_preprocessing=hparams.fused_preprocessing,
            synthetic_data=hparams.synthetic_data)
        logging.debug(validation_app_dataset.pipeline)

    cfg = configure_ipu(hparams)

    seed.set_ipu_seed(hparams.seed)

    micro_batches_per_epoch, micro_batches_per_execution, micro_batches_per_log = calculate_micro_batch_periodicity(
        hparams, batch_config, train_app_dataset.size)

    time_to_train_timer = time_to_train.TimeToTrain()

    # Create an IPU distribution strategy
    train_strategy = PopDistStrategy() if hparams.distributed_training else ipu.ipu_strategy.IPUStrategy()

    with train_strategy.scope():

        # Create an instance of the model
        model = ModelFactory.create_model(model_name=hparams.model_name,
                                          input_shape=train_app_dataset.image_shape,
                                          classes=train_app_dataset.num_classes,
                                          accelerator_side_preprocessing_fn=accelerator_side_preprocess_train_fn,
                                          eight_bit_transfer=eight_bit_transfer,
                                          norm_layer_params=hparams.norm_layer)

        # model debugging
        debug_outfeeds = []
        layers_to_debug = []
        model, debug_outfeeds = ModelFactory.debug_layers(model, debug_layers_names=layers_to_debug)

        model = ModelFactory.configure_model(model=model,
                                             gradient_accumulation_count=batch_config.gradient_accumulation_count,
                                             pipeline_splits=hparams.pipeline_splits,
                                             device_mapping=hparams.device_mapping,
                                             pipeline_schedule=hparams.pipeline_schedule,
                                             available_memory_proportion=hparams.available_memory_proportion,
                                             optimizer_state_offloading=hparams.optimizer_state_offloading)

        if hparams.training:
            # prepare the learning rate scheduler
            lr_outfeed_queue = ipu.ipu_outfeed_queue.IPUOutfeedQueue(
                outfeed_mode=ipu.ipu_outfeed_queue.IPUOutfeedMode.LAST)
            lr_scheduler = get_lr_scheduler(
                scheduler_name=hparams.lr_schedule,
                schedule_params=hparams.lr_schedule_params,
                warmup_params=hparams.lr_warmup_params,
                global_batch_size=batch_config.global_batch_size,
                weight_updates_per_epoch=hparams.weight_updates_per_epoch,
                staircase=hparams.lr_staircase,
                queue=lr_outfeed_queue if hparams.synthetic_data != 'ipu' else None
            )

            # get weight decay scheduler
            wd_scheduler = get_lr_scheduler(
                scheduler_name=hparams.lr_schedule,
                schedule_params=hparams.lr_schedule_params,
                warmup_params=hparams.lr_warmup_params,
                global_batch_size=batch_config.global_batch_size,
                weight_updates_per_epoch=hparams.weight_updates_per_epoch,
                staircase=hparams.lr_staircase,
                queue=None,
                factor=hparams.weight_decay
            )

            # prepare the optimizer
            optimizer = OptimizerFactory.get_optimizer(optimizer_name=hparams.optimizer,
                                                       optimizer_params=hparams.optimizer_params,
                                                       loss_scaling=hparams.loss_scaling,
                                                       l2_regularization=hparams.l2_regularization,
                                                       batch_config=batch_config,
                                                       lr_scheduler=lr_scheduler,
                                                       wd_scheduler=wd_scheduler,
                                                       distributed_training=hparams.distributed_training,
                                                       norm_layer_params=hparams.norm_layer)

            # prepare loss and metrics
            loss_kwargs = {'name': 'loss'}
            if hparams.label_smoothing is None:
                loss_class = tf.keras.losses.SparseCategoricalCrossentropy
            else:
                loss_class = SmoothedCategoricalCrossentropy
                loss_kwargs = dict(num_classes=train_app_dataset.num_classes, label_smoothing=hparams.label_smoothing)
            loss_outfeed_queue = ipu.ipu_outfeed_queue.IPUOutfeedQueue()

            # debug predictions and labels
            if False:
                pred_outfeed_queue = ipu.ipu_outfeed_queue.IPUOutfeedQueue()
                label_outfeed_queue = ipu.ipu_outfeed_queue.IPUOutfeedQueue()
                loss_class = wrap_loss_in_pred_enqueuer(loss_class, pred_outfeed_queue)
                loss_class = wrap_loss_in_label_enqueuer(loss_class, label_outfeed_queue)
                debug_outfeeds.append(('prediction', pred_outfeed_queue))
                debug_outfeeds.append(('label', label_outfeed_queue))

            accuracy_class = tf.keras.metrics.SparseCategoricalAccuracy
            accuracy_outfeed_queue = ipu.ipu_outfeed_queue.IPUOutfeedQueue()

            if hparams.synthetic_data != 'ipu':
                if hparams.accelerator_side_reduction:
                    loss_class = wrap_loss_in_allreduce_enqueuer(
                        loss_class, loss_outfeed_queue, num_replicas=hparams.num_replicas)
                    accuracy_class = wrap_metric_in_allreduce_enqueuer(
                        accuracy_class, accuracy_outfeed_queue, num_replicas=hparams.num_replicas)
                else:
                    loss_class = wrap_loss_in_enqueuer(loss_class, loss_outfeed_queue)
                    accuracy_class = wrap_metric_in_enqueuer(accuracy_class, accuracy_outfeed_queue)

            loss = loss_class(**loss_kwargs)
            accuracy = accuracy_class(dtype=tf.float32, name='training_accuracy')

            # Compile the model
            model.compile(loss=loss,
                          optimizer=optimizer,
                          metrics=[accuracy],
                          steps_per_execution=micro_batches_per_execution // batch_config.num_replicas)

            model.build(input_shape=(batch_config.micro_batch_size,
                                     train_app_dataset.image_shape[0],
                                     train_app_dataset.image_shape[1],
                                     train_app_dataset.image_shape[2]))
            model.summary(print_fn=logging.info)  # can't print summary until fit() or build() are invoked

            if hparams.num_pipeline_stages > 1:
                list_splits = ModelFactory.evaluate_splits(model, hparams.num_pipeline_stages)
                if hparams.pipeline_splits != list_splits:
                    logging.info(f'Recommended splits = {list_splits}')

            logging.info(f'weight_updates_per_epoch = {hparams.weight_updates_per_epoch}')
            logging.info(f'micro_batches_per_epoch = {micro_batches_per_epoch}')
            logging.info(f'micro_batches_per_execution = {micro_batches_per_execution}')
            logging.info(f'steps_per_execution = {micro_batches_per_execution // batch_config.num_replicas}')
            logging.info(f'num_epochs {hparams.num_epochs}')

            if hparams.checkpoint_dir is None:
                if hparams.distributed_training:
                    time_now = hvd.broadcast(tf.convert_to_tensor(value=time(), dtype=tf.float32), 0)
                else:
                    time_now = time()
                date_now = datetime.fromtimestamp(time_now).strftime("%d_%m_%Y_%H:%M:%S.%f")[:-3]
                hparams.checkpoint_dir = os.path.join('/tmp', 'checkpoints_' + date_now)

            if hparams.ckpt_all_instances:
                hparams.checkpoint_dir = os.path.join(hparams.checkpoint_dir, f'rank{hvd.rank()}')

            # organize the outfeed queues
            debug_outfeed_queues = [] if hparams.synthetic_data == 'ipu' else debug_outfeeds
            outfeed_queues = None if hparams.synthetic_data == 'ipu' else [('loss', loss_outfeed_queue),
                                                                           ('training_accuracy', accuracy_outfeed_queue)]

            callbacks = CallbackFactory.get_callbacks(model=model,
                                                      hyperparams=hparams,
                                                      checkpoints=hparams.checkpoints,
                                                      checkpoint_dir=hparams.checkpoint_dir,
                                                      log_period=micro_batches_per_log // batch_config.num_replicas,
                                                      images_per_execution=micro_batches_per_execution * batch_config.micro_batch_size,
                                                      micro_batches_per_epoch=batch_config.get_num_micro_batches_per_epoch(train_app_dataset.size) // batch_config.num_replicas,
                                                      debug_outfeed_queues=debug_outfeed_queues,
                                                      outfeed_queues=outfeed_queues)

            # start timer
            time_to_train_timer.start()

            # Train the model
            model.fit(train_app_dataset.pipeline,
                      steps_per_epoch=micro_batches_per_epoch // popdist.getNumInstances(),
                      epochs=hparams.num_epochs,
                      callbacks=callbacks)

        if hparams.validation:
            if not hparams.distributed_training:
                cfg.auto_select_ipus = hparams.validation_num_replicas * hparams.validation_ipus_per_replica
            else:
                if hparams.validation_num_replicas != popdist.getNumTotalReplicas() * popdist.getNumIpusPerReplica():
                    logging.warning(f'Validation replication factor given to poprun '
                                    f'(=={popdist.getNumTotalReplicas() * popdist.getNumIpusPerReplica()}) '
                                    f'does not match the config (=={hparams.validation_num_replicas}). Poprun will override the config.')

                if hparams.validation_ipus_per_replica != popdist.getNumIpusPerReplica():
                    raise ValueError(f'The number of ipus per replica in validation does not match the value provided to poprun'
                                     f'({hparams.validation_ipus_per_replica} != {popdist.getNumIpusPerReplica()})')
                popdist.tensorflow.set_ipu_config(
                    cfg, ipus_per_replica=hparams.validation_ipus_per_replica, configure_device=True)
            cfg.floating_point_behaviour.esr = ipu.config.StochasticRoundingBehaviour.from_bool(False)
            cfg.configure_ipu_system()
            seed.set_host_seed(hparams.seed)
            seed.set_ipu_seed(hparams.seed)

            # swap the training preprocess layer with inference preprocess layer
            model = replace_preprocess_layer_with_fn(model, fn=accelerator_side_preprocess_inference_fn)

            if hparams.pipeline_validation_model:
                # Gradient_accumulation_count must be changed again.
                # Configure model is also invoked to make sure the new layer has a device assignment
                model = ModelFactory.configure_model(model=model,
                                                     gradient_accumulation_count=validation_batch_config.gradient_accumulation_count,
                                                     pipeline_splits=hparams.pipeline_splits,
                                                     device_mapping=hparams.device_mapping,
                                                     pipeline_schedule=hparams.pipeline_schedule,
                                                     available_memory_proportion=hparams.available_memory_proportion,
                                                     optimizer_state_offloading=hparams.optimizer_state_offloading)

            else:
                # map all pipeline stages to one ipu and set pipeline schedule to sequential
                model.set_pipelining_options(device_mapping=[0 for _ in range(
                    len(hparams.pipeline_splits) + 1)], pipeline_schedule=pipelining_ops.PipelineSchedule.Sequential)

            validation_dataset_size = validation_app_dataset.size
            accuracy_metric_name = 'validation_accuracy'
            correct_accuracy_metric = None

            num_discarded_samples_per_instance = validation_batch_config.get_num_discarded_samples_per_instance(validation_app_dataset.size, popdist.getNumInstances())
            if num_discarded_samples_per_instance > 0:
                validation_dataset_size = validation_batch_config.get_padded_dataset_size(validation_app_dataset.size, popdist.getNumInstances())
                correct_accuracy_metric = (accuracy_metric_name, validation_dataset_size / validation_app_dataset.size)

            # Evaluate the number of steps per epoch
            validation_micro_batches_per_epoch = validation_batch_config.get_num_micro_batches_per_epoch(validation_dataset_size)
            logging.info(f'validation micro batches per epoch {validation_micro_batches_per_epoch}')
            logging.info(f'validation micro batch size {validation_batch_config.micro_batch_size}')
            logging.info(f'validation global batch size {validation_batch_config.global_batch_size}')
            logging.info(f'validation num replicas {validation_batch_config.num_replicas}')
            logging.info(f'validation dataset size {validation_dataset_size}')

            if validation_micro_batches_per_epoch == 0:
                raise ValueError(f'For validation, the number of replicas has been multiplied '
                                 f'by {hparams.num_ipus_per_replica} and then the number of validation micro batches should be '
                                 f'a multiple of {batch_config.num_replicas * hparams.num_ipus_per_replica}.')

            validation_accuracy_class = tf.keras.metrics.SparseCategoricalAccuracy
            validation_accuracy_outfeed_queue = ipu.ipu_outfeed_queue.IPUOutfeedQueue()
            if hparams.accelerator_side_reduction:
                validation_accuracy_class = wrap_metric_in_allreduce_enqueuer(
                    validation_accuracy_class,
                    validation_accuracy_outfeed_queue,
                    validation_batch_config.num_replicas
                )
            else:
                validation_accuracy_class = wrap_metric_in_enqueuer(
                    validation_accuracy_class,
                    validation_accuracy_outfeed_queue
                )
            validation_accuracy = validation_accuracy_class(name=accuracy_metric_name, dtype=tf.float32)

            # recompile the model for the validation
            model.compile(metrics=[validation_accuracy],
                          steps_per_execution=validation_micro_batches_per_epoch // validation_batch_config.num_replicas)

            validation_outfeed_queues = None if hparams.synthetic_data == 'ipu' else [
                (accuracy_metric_name, validation_accuracy_outfeed_queue)]

            validation_callbacks = CallbackFactory.get_callbacks(model=model,
                                                                 hyperparams=hparams,
                                                                 checkpoint_dir=hparams.checkpoint_dir,
                                                                 log_period=validation_micro_batches_per_epoch // validation_batch_config.num_replicas,
                                                                 images_per_execution=validation_micro_batches_per_epoch * validation_batch_config.micro_batch_size,
                                                                 micro_batches_per_epoch=validation_micro_batches_per_epoch * hparams.logs_per_epoch // batch_config.num_replicas,
                                                                 outfeed_queues=validation_outfeed_queues,
                                                                 correct_metric=correct_accuracy_metric,
                                                                 fields_to_remove=['loss'])

            ckpt_list = []
            if hparams.checkpoint_dir is not None:
                ckpt_list = glob.glob(os.path.join(hparams.checkpoint_dir, '*.h5'))
                if len(ckpt_list) == 0:
                    logging.warn(f'The directory {hparams.checkpoint_dir} doesn\'t contain checkpoint (*.h5) files')
            if len(ckpt_list) != 0:
                logging.info(f'number of checkpoints {len(ckpt_list)}')
                for ckpt_file in ckpt_list:
                    logging.info(f'checkpoint file {ckpt_file}')
                    model.load_weights(ckpt_file)
                    model.evaluate(validation_app_dataset.pipeline, steps=validation_micro_batches_per_epoch //
                                   popdist.getNumInstances(), callbacks=validation_callbacks)
                if hparams.clean_dir:
                    shutil.rmtree(hparams.checkpoint_dir)

            else:
                logging.warn(
                    'No checkpoint is used to evaluate, so it will be the last training run or random if training is false')
                metrics = model.evaluate(validation_app_dataset.pipeline, steps=validation_micro_batches_per_epoch //
                                         popdist.getNumInstances(), callbacks=validation_callbacks)

        # we only care about the TTT value if we ran both training and validation
        if hparams.training and hparams.validation:
            # stop timer
            time_to_train_timer.stop()
            time_to_train.log_time_to_train(time_to_train_timer, log_to_wandb=hparams.wandb)