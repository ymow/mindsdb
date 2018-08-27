"""
*******************************************************
 * Copyright (C) 2017 MindsDB Inc. <copyright@mindsdb.com>
 *
 * This file is part of MindsDB Server.
 *
 * MindsDB Server can not be copied and/or distributed without the express
 * permission of MindsDB Inc
 *******************************************************
"""

# import logging
from libs.helpers.logging import logging

from pymongo import MongoClient
from libs.helpers.general_helpers import convert_snake_to_cammelcase_string, get_label_index_for_value
from libs.constants.mindsdb import *
from libs.data_types.sampler import Sampler
from libs.helpers.norm_denorm_helpers import denorm
from bson.objectid import ObjectId

from libs.data_entities.persistent_model_metadata import PersistentModelMetadata
from libs.data_entities.persistent_model_metrics import PersistentModelMetrics

import importlib
import config as CONFIG
import json
import gridfs
import time
import os

class TrainWorker():
    
    def __init__(self, data, model_name, ml_model_name='pytorch.models.column_based_fcnn', config={}):
        """

        :param data:
        :param model_name:
        :param ml_model_name:
        :param config:
        """

        self.data = data
        self.model_name = model_name
        self.ml_model_name = ml_model_name
        self.config = config
        

        # get basic variables defined

        self.metadata = PersistentModelMetadata().find_one({'model_name': self.model_name})
        self.metrics = PersistentModelMetrics().find_one({'model_name': self.model_name, 'ml_model_name': self.ml_model_name})

        self.config_serialize = json.dumps(self.config)

        self.framework, self.dummy, self.data_model_name = self.ml_model_name.split('.')
        self.data_model_module_path = 'libs.data_models.' + self.ml_model_name + '.' + self.data_model_name
        self.data_model_class_name = convert_snake_to_cammelcase_string(self.data_model_name)

        self.data_model_module = importlib.import_module(self.data_model_module_path)
        self.data_model_class = getattr(self.data_model_module, self.data_model_class_name)

        self.train_sampler = Sampler(self.data[KEYS.TRAIN_SET], stats_as_stored=self.stats, ignore_types=self.data_model_class.ignore_types)
        self.test_sampler = Sampler(self.data[KEYS.TEST_SET], stats_as_stored=self.stats, ignore_types=self.data_model_class.ignore_types)

        self.train_sampler.variable_wrapper = self.data_model_class.variable_wrapper
        self.test_sampler.variable_wrapper = self.data_model_class.variable_wrapper
        self.sample_batch = self.train_sampler.getSampleBatch()

        self.gfs_save_head_time = time.time() # the last time it was saved into GridFS, assume it was now

        logging.info('Starting model...')
        self.data_model_object = self.data_model_class(self.sample_batch)
        logging.info('Training model...')
        self.train()

        
    def train(self):
        """

        :return:
        """

        last_epoch = 0
        lowest_error = None
        local_files = None

        for i in range(len(self.data_model_object.learning_rates)):

            self.data_model_object.setLearningRateIndex(i)

            for train_ret in self.data_model_object.trainModel(self.train_sampler):

                logging.info('Training State epoch:{epoch}, batch:{batch}, loss:{loss}'.format(epoch=train_ret.epoch,
                                                                                               batch=train_ret.batch,
                                                                                               loss=train_ret.loss))

                # save model every new epoch
                if last_epoch != train_ret.epoch:
                    last_epoch = train_ret.epoch
                    logging.info('New epoch:{epoch}, testing and calculating error'.format(epoch=last_epoch))
                    test_ret = self.data_model_object.testModel(self.test_sampler)
                    logging.info('Test Error:{error}'.format(error=test_ret.error))
                    is_it_lowest_error_epoch = False
                    # if lowest error save model
                    if lowest_error is None or lowest_error > test_ret.error:
                        is_it_lowest_error_epoch = True
                        lowest_error = test_ret.error
                        logging.info('Lowest ERROR so far! Saving: model {model_name}:{submodel_name}, {data_model} config:{config}'.format(
                            model_name=self.model_name, data_model=self.ml_model_name, config=self.config_serialize, submodel_name=self.submodel_name))

                        # save model local file
                        local_files = self.saveToDisk(local_files)
                        # throttle model saving into GridFS to 10 minutes
                        self.saveToGridFs(local_files, throttle=True)

                        # save model predicted - real vectors
                        logging.info('Saved: model {model_name}:{submodel_name} state vars into db [OK]'.format(model_name=self.model_name, submodel_name=self.submodel_name))

                    # check if continue training
                    if self.shouldContinue() == False:
                        return
                    # save/update model loss, error, confusion_matrix
                    self.registerModelData(train_ret, test_ret, is_it_lowest_error_epoch)

            logging.info('Loading model from store for retrain on new learning rate {lr}'.format(lr=self.data_model_object.learning_rates[i][LEARNING_RATE_INDEX]))
            # after its done with the first batch group, get the one with the lowest error and keep training

            model_state_collection = self.mongo.mindsdb.model_state.find_one(
                {'model_name': self.model_name, 'submodel_name': self.submodel_name, 'data_model': self.ml_model_name, 'config': self.config_serialize})

            if model_state_collection is None:
                # TODO: Make sure we have a model for this
                logging.info('No model found in storage')
                return

            fs_file_ids = model_state_collection['fs_file_ids']

            self.data_model_object = self.data_model_class.loadFromDisk(file_ids=fs_file_ids)




        # When out of training loop:
        # - if stop or finished leave as is (TODO: Have the hability to stop model training, but not necessarily delete it)
        #   * save best lowest error into GridFS (we only save into GridFS at the end because it takes too long)
        #   * remove local model file
        self.saveToGridFs(local_files=local_files, throttle=False)


    def registerModelData(self, train_ret, test_ret, lowest_error_epoch = False):
        """
        This method updates stats about the model, it's called on each epoch

        Stores:
            - loss
            - error
            - confusion matrices

        :param train_ret    The result of training a batch
        :param test_ret     The result of testing after an epoch
        :param lowest_error_epoch   Is this epoch the one with the lowest error so far
        """

        # #########
        # STORE TRAIN STATS
        #
        # Note: The primary key here is composed of the model_name, the data_model and the config
        #       We do this so we can train multiple data_models per model
        #
        # TODO: Find better naming for either model or data_model as it can be confusing
        #       - suggestions for model, given that model is defined as: athing that we have to: from X predict Y
        # ########

        primary_key = {
                'model_name': self.model_name,
                'submodel_name': self.submodel_name,
                'data_model': self.ml_model_name,
                'config': self.config_serialize
            }

        # Operations that happen regardless of it being or not a lowest error epoch or not
        operations = {
            '$push': {
               "loss_y": train_ret.loss,
               "loss_x": train_ret.epoch,
               "error_y": test_ret.error,
               "error_x": train_ret.epoch
            }
        }

        if lowest_error_epoch == True:
            # #########
            # CALCULATE THE CONFUSION MATRIX
            # ########

            # denorm the real and predicted
            predicted_targets = {col:[denorm(row, self.stats['stats'][col]) for row in test_ret.predicted_targets[col]] for col in test_ret.predicted_targets}
            real_targets = {col: [denorm(row, self.stats['stats'][col]) for row in test_ret.real_targets[col]] for col in test_ret.real_targets}
            # confusion matrices with zeros
            confusion_matrices = {
                col: {
                    'labels': [ label for label in self.stats['stats'][col]['histogram']['x'] ],
                    'real_x_predicted_dist': [ [ 0 for i in self.stats['stats'][col]['histogram']['x']] for j in self.stats['stats'][col]['histogram']['x'] ],
                    'real_x_predicted': [[0 for i in self.stats['stats'][col]['histogram']['x']] for j in self.stats['stats'][col]['histogram']['x']]
                }
                for col in real_targets
            }
            for col in real_targets:
                reduced_buckets = []
                labels = confusion_matrices[col]['labels']
                for i,label in enumerate(labels):
                    index = int(i) + 1
                    if index % 5 == 0:
                        reduced_buckets.append(int(labels[i]))

                reduced_confusion_matrices = {
                    col:{
                        'labels':reduced_buckets,
                        'real_x_predicted_dist':[[0 for i in reduced_buckets] for j in reduced_buckets],
                        'real_x_predicted':[[0 for i in reduced_buckets] for j in reduced_buckets]
                    }
                }

            # calculate confusion matrices real vs predicted
            for col in predicted_targets:
                totals = [0]*len(self.stats['stats'][col]['histogram']['x'])
                reduced_totals = [0]*len(reduced_buckets)
                for i, predicted_value in enumerate(predicted_targets[col]):
                    predicted_index = get_label_index_for_value(predicted_value, confusion_matrices[col]['labels'])
                    real_index = get_label_index_for_value(real_targets[col][i], confusion_matrices[col]['labels'])
                    confusion_matrices[col]['real_x_predicted_dist'][real_index][predicted_index] += 1
                    totals[predicted_index] += 1

                    reduced_predicted_index = get_label_index_for_value(predicted_value, reduced_confusion_matrices[col]['labels'])
                    reduced_real_index = get_label_index_for_value(real_targets[col][i], reduced_confusion_matrices[col]['labels'])
                    reduced_confusion_matrices[col]['real_x_predicted_dist'][reduced_real_index][reduced_predicted_index] += 1
                    reduced_totals[reduced_predicted_index] += 1

                # calculate probability of predicted being correct P(predicted=real|predicted)
                for pred_j, label in  enumerate(confusion_matrices[col]['labels']):
                    for real_j, label  in enumerate(confusion_matrices[col]['labels']):
                        if totals[pred_j] == 0:
                            confusion_matrices[col]['real_x_predicted'][real_j][pred_j] = 0
                        else:
                            confusion_matrices[col]['real_x_predicted'][real_j][pred_j] = confusion_matrices[col]['real_x_predicted_dist'][real_j][pred_j] / totals[pred_j]

                for pred_j, label in  enumerate(reduced_confusion_matrices[col]['labels']):
                    for real_j, label  in enumerate(reduced_confusion_matrices[col]['labels']):
                        if reduced_totals[pred_j] == 0:
                            reduced_confusion_matrices[col]['real_x_predicted'][real_j][pred_j] = 0
                        else:
                            reduced_confusion_matrices[col]['real_x_predicted'][real_j][pred_j] = reduced_confusion_matrices[col]['real_x_predicted_dist'][real_j][pred_j] / reduced_totals[pred_j]

            operations['$set'] = {}
            operations['$set']['lowest_error'] = test_ret.error
            operations['$set']['predicted_targets'] =  predicted_targets
            operations['$set']['real_targets'] =  real_targets
            operations['$set']['confusion_matrices'] = confusion_matrices
            operations['$set']['reduced_confusion_matrices'] =reduced_confusion_matrices
            operations['$set']['accuracy'] = test_ret.accuracy

        # save model train stats and push data do it
        model_stats = self.mongo.mindsdb.model_train_stats
        model = model_stats.find_one(primary_key)
        if not model:
            insert_key  = primary_key.copy()
            insert_key['_id'] = str(ObjectId())
            model_stats.insert(insert_key)

        model_stats.update_one(primary_key, operations, upsert=True)

        return True

    def shouldContinue(self):
        """
        Check if the training should continue
        :return:
        """

        model_name = self.model_name

        # check if stop training is set in which case we should exit the training

        model = self.mongo.mindsdb.model_stats.find_one({'model_name': model_name})

        if model and STOP_TRAINING in model and model[STOP_TRAINING] == True:
            return False

        if model and KILL_TRAINING in model and model[KILL_TRAINING] == True:
            logging.info('[FORCED] Stopping model training....')
            model_stats = self.mongo.mindsdb.model_stats
            model_stats.delete_many({'model_name': model_name, 'submodel_name': self.submodel_name})
            model_state = self.mongo.mindsdb.model_state
            model_state.delete_many({'model_name': model_name, 'submodel_name': self.submodel_name})
            model_train_stats = self.mongo.mindsdb.model_train_stats
            model_train_stats.delete_many({'model_name': model_name, 'submodel_name': self.submodel_name})
            return False

        return True

    def saveToDisk(self, local_files):
        """
        This method persists model into disk, and removes previous stored files of this model

        :param local_files: any previous files
        :return:
        """
        if local_files is not None:
            for file_response_object in local_files:
                try:
                    os.remove(file_response_object.path)
                except:
                    logging.info('Could not delete file {path}'.format(path=file_response_object.path))

        return_objects =  self.data_model_object.saveToDisk()

        file_ids = [ret.file_id for ret in return_objects]

        self.mongo.mindsdb.model_state.update_one(
            {'model_name': self.model_name, 'submodel_name': self.submodel_name, 'data_model': self.ml_model_name, 'config': self.config_serialize},
            {'$set': {
                "model_name": self.model_name,
                'submodel_name': self.submodel_name,
                'data_model': self.ml_model_name,
                'config': self.config_serialize,
                "fs_file_ids": file_ids
            }}, upsert=True)

        return return_objects


    def saveToGridFs(self, local_files, throttle = False):
        """
        This method is to save to the gridfs local files

        :param local_files:
        :param throttle:
        :return:
        """
        current_time = time.time()

        if throttle == True or local_files is None or len(local_files) == 0:

            if (current_time - self.gfs_save_head_time) < 60 * 10:
                logging.info('Not saving yet, throttle time not met')
                return

        # if time met, save to GFS
        self.gfs_save_head_time = current_time

        # delete any existing files if they exist
        model_state = self.mongo.mindsdb.model_state.find_one({'model_name': self.model_name, 'submodel_name': self.submodel_name, 'data_model': self.ml_model_name, 'config': self.config_serialize})
        if model_state and 'gridfs_file_ids' in model_state:
            for file_id in model_state['gridfs_file_ids']:
                try:
                    self.mongo_gfs.delete(file_id)
                except:
                    logging.warning('could not delete gfs {file_id}'.format(file_id=file_id))

        file_ids = []
        # save into gridfs
        for file_response_object in local_files:
            logging.info('Saving file into GridFS, this may take a while ...')
            file_id = self.mongo_gfs.put(open(file_response_object.path, "rb").read())
            file_ids += [file_id]

        logging.info('[DONE] files into GridFS saved')
        self.mongo.mindsdb.model_state.update_one({'model_name': self.model_name, 'submodel_name': self.submodel_name, 'data_model': self.ml_model_name, 'config': self.config_serialize},
                                                  {'$set': {
                                   "model_name": self.model_name,
                                   'submodel_name': self.submodel_name,
                                   'data_model': self.ml_model_name,
                                   'config': self.config_serialize,
                                   "gridfs_file_ids": file_ids
                               }}, upsert=True)

    
    @staticmethod
    def start(data, model_name, ml_model, config={}):
        """
        We use this worker to parallel train different data models and data model configurations
    
        :param data: This is the vectorized data
        :param model_name: This will be the model name so we can pull stats and other
        :param ml_model: This will be the data model name, which can let us find the data model implementation
        :param config: this is the hyperparameter config
        """

        return TrainWorker(data, model_name, ml_model, config)


# TODO: Use ray
# @ray.remote
# def rayRun(**kwargs)
#     TrainWorker.start(**kwargs)