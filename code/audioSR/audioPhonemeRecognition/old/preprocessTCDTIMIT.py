import cPickle
import glob
import math
import os
import timeit;
import numpy as np
import scipy.io.wavfile as wav
from tqdm import tqdm

program_start_time = timeit.default_timer()
import random
random.seed(int(timeit.default_timer()))
import pdb

import python_speech_features
import logging, audioPhonemeRecognition.formatting

logger = logging.getLogger('PrepTCDTIMIT')
logger.setLevel(logging.DEBUG)
FORMAT = '[$BOLD%(filename)s$RESET:%(lineno)d][%(levelname)-5s]: %(message)s '
formatter = logging.Formatter(audioPhonemeRecognition.formatting.formatter_message(FORMAT, False))

# create console handler with a higher log level
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(formatter)
logger.addHandler(ch)

# File logger: see below META VARIABLES
from audioPhonemeRecognition.phoneme_set import phoneme_set_39_list, phoneme_set_61_list
import audioPhonemeRecognition.general_tools
import audioPhonemeRecognition.preprocessWavs

##### SCRIPT META VARIABLES #####
VERBOSE = True
DEBUG = False
debug_size = 50

# TRAIN/ VAL/ TEST.   TOTAL = TRAINING + TEST = TRAIN + VAL + TEST
FRAC_TEST = 0.1
FRAC_TRAINING = 1 - FRAC_TEST
FRAC_VAL = 0.1

# TODO:  MODIFY THESE PARAMETERS for other nbPhonemes. Save location is updated automatically.
nbPhonemes = 39
phoneme_set_list = phoneme_set_39_list  # import list of phonemes,
# convert to dictionary with number mappings (see phoneme_set.py)
values = [i for i in range(0, len(phoneme_set_list))]
phoneme_classes = dict(zip(phoneme_set_list, values))

## DATA LOCATIONS ##

data_source_path = os.path.expanduser("~/TCDTIMIT/audioSR/TCDTIMITaudio_resampled/fixed"+str(nbPhonemes)+"/data_volunteers")

outputDir = "/home/matthijs/TCDTIMIT/audioSR/TCDTIMITaudio_resampled/binary" + str(nbPhonemes) + os.sep + os.path.basename(data_source_path)
target = os.path.join(outputDir, os.path.basename(data_source_path)+'_26_ch'); target_path = target + '.pkl'
if not os.path.exists(outputDir):
    os.makedirs(outputDir)


# Already exists, ask if overwrite
if (os.path.exists(target_path)):
    if (not audioPhonemeRecognition.general_tools.query_yes_no(target_path + " exists. Overwrite?", "no")):
        raise Exception("Not Overwriting")


# set log file
logFile = outputDir + os.sep + os.path.basename(data_source_path) +'.log'
fh = logging.FileHandler(logFile, 'w')  # create new logFile
fh.setLevel(logging.DEBUG)
fh.setFormatter(formatter)
logger.addHandler(fh)

##### The PREPROCESSING itself #####

logger.info('Preprocessing data ...')
logger.info('  Data: %s ', data_source_path)
X_all, y_all = audioPhonemeRecognition.preprocessWavs.preprocess_dataset(source_path=data_source_path, logger=logger)

assert len(X_all) == len(y_all)
logger.info(' Loading data complete.')


logger.debug('Type and shape/len of X_all')
logger.debug('type(X_all): {}'.format(type(X_all)))
logger.debug('type(X_all[0]): {}'.format(type(X_all[0])))
logger.debug('type(X_all[0][0]): {}'.format(type(X_all[0][0])))
logger.debug('type(X_all[0][0][0]): {}'.format(type(X_all[0][0][0])))

logger.info('Creating Validation index ...')
total_size = len(X_all)                                             # TOTAL = TRAINING + TEST = TRAIN + VAL + TEST
total_training_size = int(math.ceil(FRAC_TRAINING * total_size))    # TRAINING = TRAIN + VAL
test_size = total_size - total_training_size                  
val_size = int(math.ceil(total_training_size * FRAC_VAL))
train_size = total_training_size - val_size


# FIRST, split off a 'test' dataset
test_idx = random.sample(range(0, total_training_size), test_size)
test_idx = [int(i) for i in test_idx]

# ensure that the testidation set isn't empty
if DEBUG:
    test_idx[0] = 0
    test_idx[1] = 1

logger.info('Separating test and training set ...')
X_training = []
X_test = []
y_training = []
y_test = []
for i in range(len(X_all)):
    if i in test_idx:
        X_test.append(X_all[i])
        y_test.append(y_all[i])
    else:
        X_training.append(X_all[i])
        y_training.append(y_all[i])

assert len(X_test) == test_size
assert len(X_training) == total_training_size


# SECOND, split off a 'validation' set from the training set. The remainder is the 'train' set
val_idx = random.sample(range(0, total_training_size), val_size)
val_idx = [int(i) for i in val_idx]

# ensure that the validation set isn't empty
if DEBUG:
    val_idx[0] = 0
    val_idx[1] = 1

logger.info('Separating training set into validation and train ...')
X_train = []
X_val = []
y_train = []
y_val = []
for i in range(len(X_training)):
    if i in val_idx:
        X_val.append(X_training[i])
        y_val.append(y_training[i])
    else:
        X_train.append(X_training[i])
        y_train.append(y_training[i])
assert len(X_val) == val_size


# Print some information
logger.info('Length of train, val, test')
logger.info("  train X: %s", len(X_train))
logger.info("  train y: %s", len(y_train))

logger.info("  val X: %s", len(X_val))
logger.info("  val y: %s", len(y_val))

logger.info("  test X: %s", len(X_test))
logger.info("  test y: %s", len(y_test))


# Normalize data
logger.info('Normalizing data ...')
logger.info('    Each channel mean=0, sd=1 ...')

mean_val, std_val, _ = audioPhonemeRecognition.preprocessWavs.calc_norm_param(X_train)

X_train = audioPhonemeRecognition.preprocessWavs.normalize(X_train, mean_val, std_val)
X_val = audioPhonemeRecognition.preprocessWavs.normalize(X_val, mean_val, std_val)
X_test = audioPhonemeRecognition.preprocessWavs.normalize(X_test, mean_val, std_val)

# make sure we're working with float32
X_data_type = 'float32'
X_train = audioPhonemeRecognition.preprocessWavs.set_type(X_train, X_data_type)
X_val = audioPhonemeRecognition.preprocessWavs.set_type(X_val, X_data_type)
X_test = audioPhonemeRecognition.preprocessWavs.set_type(X_test, X_data_type)

y_data_type = 'int32'
y_train = audioPhonemeRecognition.preprocessWavs.set_type(y_train, y_data_type)
y_val = audioPhonemeRecognition.preprocessWavs.set_type(y_val, y_data_type)
y_test = audioPhonemeRecognition.preprocessWavs.set_type(y_test, y_data_type)

# Convert to numpy arrays
# X_train = np.array(X_train)
# X_val = np.array(X_val)
# X_test = np.array(X_test)
#
# y_train = np.array(y_train)
# y_val = np.array(y_val)
# y_test = np.array(y_test)


logger.debug('X train')
logger.debug('  %s %s', type(X_train), len(X_train))
logger.debug('  %s %s', type(X_train[0]), X_train[0].shape)
logger.debug('  %s %s', type(X_train[0][0]), X_train[0][0].shape)
logger.debug('y train')
logger.debug('  %s %s', type(y_train), len(y_train))
logger.debug('  %s %s', type(y_train[0]), y_train[0].shape)
logger.debug('  %s %s', type(y_train[0][0]), y_train[0][0].shape)


logger.info('Saving data to %s', target_path)
dataList = [X_train, y_train, X_val, y_val, X_test, y_test, mean_val, std_val]
audioPhonemeRecognition.general_tools.saveToPkl(target_path, dataList)

meanStd_path = os.path.dirname(outputDir) + os.sep + os.path.basename(data_source_path) + "MeanStd.pkl"
logger.info('Saving Mean and Std_val to %s', meanStd_path)
dataList = [mean_val, std_val]
audioPhonemeRecognition.general_tools.saveToPkl(meanStd_path, dataList)

logger.info('Preprocessing complete!')

logger.info('Total time: {:.3f}'.format(timeit.default_timer() - program_start_time))
