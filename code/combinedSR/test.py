import os
import logging, formatting
logger_combined = logging.getLogger('combined')
logger_combined.setLevel(logging.DEBUG)
FORMAT = '[$BOLD%(filename)s$RESET:%(lineno)d][%(levelname)-5s]: %(message)s '
formatter = logging.Formatter(formatting.formatter_message(FORMAT, False))


dataset = "TCDTIMIT"
root_dir = os.path.expanduser("~/TCDTIMIT/combinedSR/")
store_dir = root_dir + dataset + "/results"
if not os.path.exists(store_dir): os.makedirs(store_dir)

if not os.path.exists(store_dir): os.makedirs(store_dir)
database_binaryDir = root_dir + dataset + '/binary'
processedDir = database_binaryDir + "_finalProcessed"
datasetType = "combined";


# just get the names
testVolunteerNumbers = ["13F", "15F", "21M", "23M", "24M", "25M", "28M", "29M", "30F", "31F", "34M", "36F", "37F", "43F", "47M", "51F", "54M"];
testVolunteers = sorted([str(testNumber) + ".pkl" for testNumber in testVolunteerNumbers])
lipspeakers = ["Lipspkr1.pkl", "Lipspkr2.pkl", "Lipspkr3.pkl"];
allSpeakers = sorted([f for f in os.listdir(database_binaryDir) if
               os.path.isfile(os.path.join(database_binaryDir, f)) and os.path.splitext(f)[1] == ".pkl"])
trainVolunteers = sorted([f for f in allSpeakers if not (f in testVolunteers or f in lipspeakers)])

if datasetType == "combined":
    trainingSpeakerFiles = trainVolunteers + lipspeakers
    testSpeakerFiles = testVolunteers
elif datasetType == "volunteers":
    trainingSpeakerFiles = trainVolunteers
datasetFiles = [sorted(trainingSpeakerFiles), sorted(testSpeakerFiles)]


## TEST split train/val/test
import preprocessingCombined
train, val, test = preprocessingCombined.getOneSpeaker(trainingSpeakerFiles[0],
                                                       sourceDataDir=database_binaryDir,
                                                       storeProcessed=False, processedDir=processedDir,
                                                       trainFraction=1.0, validFraction=0.0, verbose=True)

images, mfccs, audioLabels, validLabels, validAudioFrames = train
import pdb;pdb.set_trace()

import numpy as np
validAudioFrames = np.reshape(validAudioFrames[0], (1, validAudioFrames[0].shape))
