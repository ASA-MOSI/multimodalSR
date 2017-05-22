from __future__ import print_function

import logging  # debug < info < warn < error < critical  # from https://docs.python.org/3/howto/logging-cookbook.html
import traceback

import theano
import theano.tensor as T
from tqdm import tqdm

logger_combinedtools = logging.getLogger('combined.tools')
logger_combinedtools.setLevel(logging.DEBUG)

from general_tools import *
import os
import time
import lasagne
import lasagne.layers as L
import lasagne.objectives as LO
import numpy as np
import preprocessingCombined


class NeuralNetwork:
    network = None
    training_fn = None
    best_param = None
    best_error = 100
    curr_epoch, best_epoch = 0, 0
    X = None
    Y = None

    network_train_info = [[], [], []]

    def __init__(self, architecture, data=None, loadPerSpeaker = True, dataset="TCDTIMIT", test_dataset="TCDTIMIT",
                 batch_size=1, num_features=39, num_output_units=39,
                 lstm_hidden_list=(100,), bidirectional=True,
                 cnn_network="google", cnn_features='dense', lipRNN_hidden_list=None, lipRNN_bidirectional=True, lipRNN_features="rawRNNfeatures",
                 dense_hidden_list=(512,), save_name=None, audioNet_features='lstm',
                 seed=int(time.time()), model_paths={}, debug=False, verbose=False, logger=logger_combinedtools):

        self.dataset= dataset
        self.test_dataset = test_dataset
        self.loadPerSpeaker = loadPerSpeaker
        self.model_paths = model_paths

        self.num_output_units = num_output_units
        self.num_features = num_features
        self.batch_size = batch_size
        self.epochsNotImproved = 0  # keep track, to know when to stop training


        # for storage of training info
        self.network_train_info = {
            'train_cost': [],
            'val_cost':   [], 'val_acc': [], 'val_topk_acc': [],
            'test_cost':  [], 'test_acc': [], 'test_topk_acc': [],
            'nb_params': {}
        }  # used to be list of lists

        if architecture == "combined":
            if data != None:
                images_train, mfccs_train, audioLabels_train, validLabels_train, validAudioFrames_train = data

                # import pdb;pdb.set_trace()

                self.images = images_train[0]  # images are stored per video file. batch_size is for audio
                self.mfccs = mfccs_train[:batch_size]
                self.audioLabels = audioLabels_train[:batch_size]
                self.validLabels = validLabels_train[:batch_size]
                self.validAudioFrames = validAudioFrames_train[:batch_size]

                # import pdb;pdb.set_trace()
                self.masks = generate_masks(inputs=self.mfccs, valid_frames=self.validAudioFrames,
                                            batch_size=len(self.mfccs),
                                            logger=logger_combinedtools)
                self.mfccs = pad_sequences_X(self.mfccs)  # shouldn't change shape because batch_size == 1
                self.audioLabels = pad_sequences_y(self.audioLabels)  # these aren't actually used
                self.validLabels = pad_sequences_y(self.validLabels)
                self.validAudioFrames = pad_sequences_y(self.validAudioFrames)

                if verbose:
                    logger.debug('images.shape:          %s', len(self.images))
                    logger.debug('images[0].shape:       %s', self.images[0].shape)
                    logger.debug('images[0][0][0].type:  %s', type(self.images[0][0][0]))
                    logger.debug('y.shape:          %s', self.audioLabels.shape)
                    logger.debug('y[0].shape:       %s', self.audioLabels[0].shape)
                    logger.debug('y[0][0].type:     %s', type(self.audioLabels[0][0]))
                    logger.debug('masks.shape:      %s', self.masks.shape)
                    logger.debug('masks[0].shape:   %s', self.masks[0].shape)
                    logger.debug('masks[0][0].type: %s', type(self.masks[0][0]))

            logger.info("NUM FEATURES: %s", num_features)

            # create Theano variables and generate the networks
            self.LR_var = T.scalar('LR', dtype=theano.config.floatX)
            self.targets_var = T.imatrix('targets')  # 2D for the RNN (1 many frames (and targets) per example)
            self.CNN_targets_var = T.ivector('targets')  # 1D for the CNN (1 target per example)


            ## AUDIO PART ##
            self.audio_inputs_var = T.tensor3('audio_inputs')
            self.audio_masks_var = T.matrix('audio_masks')
            self.audio_valid_frames_var = T.imatrix('valid_indices')

            self.audioNet_dict, self.audioNet_lout, self.audioNet_lout_flattened, self.audioNet_lout_features = \
                self.build_audioRNN(n_hidden_list=lstm_hidden_list, bidirectional=bidirectional,
                                    seed=seed, debug=debug, logger=logger)
            # audioNet_lout_flattened output shape: (nbValidFrames, 39)
            # pass on 39 phoneme predictions instead of nbLSTMunits features
            if audioNet_features: self.audioNet_lout_features = self.audioNet_lout


            ## LIPREADING PART ##
            self.CNN_input_var = T.tensor4('cnn_input')
            # batch size is number of valid frames in each video
            if "google" in cnn_network:
                if "binary" in cnn_network:
                    self.CNN_dict, self.CNN_lout, self.CNN_lout_features = self.build_google_binary_CNN()
                else:
                    self.CNN_dict, self.CNN_lout, self.CNN_lout_features = self.build_google_CNN()
            elif "resnet50" in cnn_network:
                self.CNN_dict, self.CNN_lout, self.CNN_lout_features = self.build_resnet50_CNN()
            elif "cifar10_v2" in cnn_network:
                self.CNN_dict, self.CNN_lout, self.CNN_lout_features = self.build_cifar10_CNN_v2()
            elif "cifar10" in cnn_network:
                self.CNN_dict, self.CNN_lout, self.CNN_lout_features = self.build_cifar10_CNN_v2()

            # CNN_lout_features output shape = (nbValidFrames, 512x7x7)

            self.cnn_features = cnn_features
            # for CNN-LSTM combination networks
            self.lipreadingType = 'CNN'
            if lipRNN_hidden_list != None:  #add LSTM layers on top of the CNN
                self.lipreadingType = 'CNN_LSTM'
                # input to LSTM network: conv features, or with dense softmax layer in between?
                # direct conv outputs is 512x7x7 = 25.088 features -> huge networks. Might need to reduce size
                if cnn_features == 'dense':
                    self.lipreadingRNN_dict, self.lipreading_lout_features = self.build_lipreadingRNN(self.CNN_lout,
                                                                                                      lipRNN_hidden_list,
                                                                                                      bidirectional=lipRNN_bidirectional)
                else:
                    self.lipreadingRNN_dict, self.lipreading_lout_features = self.build_lipreadingRNN(self.CNN_lout_features,
                                                                                                      lipRNN_hidden_list,
                                                                                                      bidirectional=lipRNN_bidirectional)
                # For lipreading only: input to softmax FC layer now not from conv layer, but from LSTM features that are put on top of the CNNs
                self.lipreading_lout = self.build_softmax(inputLayer = self.lipreading_lout_features, nbClasses=self.num_output_units)
                if lipRNN_features == 'dense':
                    self.lipreading_lout_features = self.lipreading_lout

            else:  #only use the CNN
                if cnn_features == 'dense':
                    self.lipreading_lout_features = self.CNN_lout
                else:
                    self.lipreading_lout_features = self.CNN_lout_features
                self.lipreading_lout = self.CNN_lout

                # # You can use this to get the shape of the raw features (before FC layers), which needs to be hard-coded in the build_<networkName>() function
                # logger_combinedtools.debug("lip features shape: %s", self.lipreading_lout_features.output_shape)
                # import pdb;pdb.set_trace()


            ## COMBINED PART ##
            # batch size is number of valid frames in each video
            self.combined_dict, self.combined_lout = self.build_combined(lipreading_lout=self.lipreading_lout_features,
                                                                        audio_lout=self.audioNet_lout_features,
                                                                        dense_hidden_list=dense_hidden_list)

            self.loadPreviousResults(save_name)
            nb_params = self.getParamsInfo()
            self.network_train_info['nb_params'] = nb_params
            store_path = save_name + '_trainInfo.pkl'
            saveToPkl(store_path, self.network_train_info)

            logger_combinedtools.info(" # params lipreading seperate: %s", "{:,}".format(nb_params['nb_lipreading']))
            logger_combinedtools.info(" # params audio seperate:      %s", "{:,}".format(nb_params['nb_audio']))

            logger_combinedtools.info(" # params combining: ")
            logger_combinedtools.info(" # params total:            %s", "{:,}".format(nb_params['nb_total']))
            logger_combinedtools.info(" # params lip features:     %s", "{:,}".format(nb_params['nb_lipreading_features']))
            logger_combinedtools.info(" # params CNN features:     %s", "{:,}".format(nb_params['nb_CNN_used']))
            logger_combinedtools.info(" # params lip LSTM:         %s", "{:,}".format(nb_params['nb_lipRNN']))
            logger_combinedtools.info(" # params audio features:   %s", "{:,}".format(nb_params['nb_audio_features']))
            logger_combinedtools.info(" # params combining FC:     %s", "{:,}".format(nb_params['nb_combining']))

            # allLayers= L.get_all_layers(self.lipreading_lout)
            # for layer in allLayers:
            #     logger_combinedtools.debug("layer : %s \t %s", layer, layer.output_shape)
            # [layer.output_shape for layer in allLayers[-5:-1]]
            #import pdb;pdb.set_trace()


        else:
            print("ERROR: Invalid argument: The valid architecture arguments are: 'combined'")
        
        
    def build_audioRNN(self, n_hidden_list=(100,), bidirectional=False,
                       seed=int(time.time()), debug=False, logger=logger_combinedtools):
        # some inspiration from http://colinraffel.com/talks/hammer2015recurrent.pdf

        if debug:
            logger.debug('\nInputs:');
            logger.debug('  X.shape:    %s', self.mfccs[0].shape)
            logger.debug('  X[0].shape: %s %s %s \n%s', self.mfccs[0][0].shape, type(self.mfccs[0][0]),
                         type(self.mfccs[0][0][0]), self.mfccs[0][0][:5])

            logger.debug('Targets: ');
            logger.debug('  Y.shape:    %s', self.validLabels.shape)
            logger.debug('  Y[0].shape: %s %s %s \n%s', self.validLabels[0].shape, type(self.validLabels[0]),
                         type(self.validLabels[0][0]),
                         self.validLabels[0][:5])
            logger.debug('Layers: ')

        # fix these at initialization because it allows for compiler opimizations
        num_output_units = self.num_output_units
        num_features = self.num_features
        batch_size = self.batch_size

        audio_inputs = self.audio_inputs_var
        audio_masks = self.audio_masks_var  # set MATRIX, not iMatrix!! Otherwise all mask calculations are done by CPU, and everything will be ~2x slowed down!! Also in general_tools.generate_masks()
        valid_frames = self.audio_valid_frames_var

        net = {}

        # shape = (batch_size, batch_max_seq_length, num_features)
        net['l1_in'] = L.InputLayer(shape=(batch_size, None, num_features), input_var=audio_inputs)
        net['l1_mask'] = L.InputLayer(shape=(batch_size, None), input_var=audio_masks)

        if debug:
            get_l_in = L.get_output(net['l1_in'])
            l_in_val = get_l_in.eval({net['l1_in'].input_var: self.mfccs})
            # logger.debug(l_in_val)
            logger.debug('  l_in size: %s', l_in_val.shape);

            get_l_mask = L.get_output(net['l1_mask'])
            l_mask_val = get_l_mask.eval({net['l1_mask'].input_var: self.masks})
            # logger.debug(l_in_val)
            logger.debug('  l_mask size: %s', l_mask_val.shape);

            n_batch, n_time_steps, n_features = net['l1_in'].input_var.shape
            logger.debug("  n_batch: %s | n_time_steps: %s | n_features: %s", n_batch, n_time_steps,
                         n_features)

        ## LSTM parameters
        gate_parameters = L.recurrent.Gate(
                W_in=lasagne.init.Orthogonal(), W_hid=lasagne.init.Orthogonal(),
                b=lasagne.init.Constant(0.))
        cell_parameters = L.recurrent.Gate(
                W_in=lasagne.init.Orthogonal(), W_hid=lasagne.init.Orthogonal(),
                # Setting W_cell to None denotes that no cell connection will be used.
                W_cell=None, b=lasagne.init.Constant(0.),
                # By convention, the cell nonlinearity is tanh in an LSTM.
                nonlinearity=lasagne.nonlinearities.tanh)

        # generate layers of stacked LSTMs, possibly bidirectional
        net['l2_lstm'] = []

        for i in range(len(n_hidden_list)):
            n_hidden = n_hidden_list[i]

            if i == 0:
                input = net['l1_in']
            else:
                input = net['l2_lstm'][i - 1]

            nextForwardLSTMLayer = L.recurrent.LSTMLayer(
                    input, n_hidden,
                    # We need to specify a separate input for masks
                    mask_input=net['l1_mask'],
                    # Here, we supply the gate parameters for each gate
                    ingate=gate_parameters, forgetgate=gate_parameters,
                    cell=cell_parameters, outgate=gate_parameters,
                    # We'll learn the initialization and use gradient clipping
                    learn_init=True, grad_clipping=100.)
            net['l2_lstm'].append(nextForwardLSTMLayer)

            if bidirectional:
                input = net['l2_lstm'][-1]
                # Use backward LSTM
                # The "backwards" layer is the same as the first,
                # except that the backwards argument is set to True.
                nextBackwardLSTMLayer = L.recurrent.LSTMLayer(
                        input, n_hidden, ingate=gate_parameters,
                        mask_input=net['l1_mask'], forgetgate=gate_parameters,
                        cell=cell_parameters, outgate=gate_parameters,
                        learn_init=True, grad_clipping=100., backwards=True)
                net['l2_lstm'].append(nextBackwardLSTMLayer)

                # We'll combine the forward and backward layer output by summing.
                # Merge layers take in lists of layers to merge as input.
                # The output of l_sum will be of shape (n_batch, max_n_time_steps, n_features)
                net['l2_lstm'].append(L.ElemwiseSumLayer([net['l2_lstm'][-2], net['l2_lstm'][-1]]))

        # we need to convert (batch_size, seq_length, num_features) to (batch_size * seq_length, num_features) because Dense networks can't deal with 2 unknown sizes
        net['l3_reshape'] = L.ReshapeLayer(net['l2_lstm'][-1], (-1, n_hidden_list[-1]))

        # Get the output features for passing to the combination network
        net['l4_features'] = L.SliceLayer(net['l3_reshape'], indices=valid_frames, axis=0)
        net['l4_features'] = L.ReshapeLayer(net['l4_features'], (-1, n_hidden_list[-1]))

        # this will output shape(nbValidFrames, nbLSTMunits)


        # add some extra layers to get an output for the audio network only
        # Now we can apply feed-forward layers as usual for classification
        net['l6_dense'] = L.DenseLayer(net['l3_reshape'], num_units=num_output_units,
                                       nonlinearity=lasagne.nonlinearities.softmax)

        # # Now, the shape will be (n_batch * n_timesteps, num_output_units). We can then reshape to
        # # n_batch to get num_output_units values for each timestep from each sequence
        # only use the valid indices
        net['l7_out'] = L.ReshapeLayer(net['l6_dense'], (batch_size, -1, num_output_units))
        net['l7_out_valid_basic'] = L.SliceLayer(net['l7_out'], indices=valid_frames, axis=1)
        net['l7_out_valid_flattened'] = L.ReshapeLayer(net['l7_out_valid_basic'], (-1, num_output_units))
        net['l7_out_valid'] = L.ReshapeLayer(net['l7_out_valid_basic'], (batch_size, -1, num_output_units))

        if debug:
            get_l_out = theano.function([net['l1_in'].input_var, net['l1_mask'].input_var], L.get_output(net['l7_out']))
            l_out = get_l_out(self.mfccs, self.masks)

            # this only works for batch_size == 1
            get_l_out_valid = theano.function([audio_inputs, audio_masks, valid_frames],
                                              L.get_output(net['l7_out_valid']))
            try:
                l_out_valid = get_l_out_valid(self.mfccs, self.masks, self.validAudioFrames)
                logger.debug('\n\n\n  l_out: %s  | l_out_valid: %s', l_out.shape, l_out_valid.shape);
            except:
                logger.warning("batchsize not 1, get_valid not working")

        if debug:   self.print_RNN_network_structure(net)

        if debug:import pdb;pdb.set_trace()

        return net, net['l7_out_valid'], net['l7_out_valid_flattened'], net['l4_features']

    # network from Oxford & Google BBC paper
    def build_google_CNN(self, input=None, activation=T.nnet.relu, alpha=0.1, epsilon=1e-4):
        input = self.CNN_input_var
        nbClasses = self.num_output_units

        cnnDict = {}
        # input
        # store each layer of the network in a dict, for quickly retrieving any layer
        cnnDict['l0_in'] = lasagne.layers.InputLayer(
                shape=(None, 1, 120, 120),  # 5,120,120 (5 = #frames)
                input_var=input)

        cnnDict['l1_conv1'] = []
        cnnDict['l1_conv1'].append(lasagne.layers.Conv2DLayer(
                cnnDict['l0_in'],
                num_filters=128,
                filter_size=(3, 3),
                pad=1,
                nonlinearity=lasagne.nonlinearities.identity))
        cnnDict['l1_conv1'].append(lasagne.layers.MaxPool2DLayer(cnnDict['l1_conv1'][-1], pool_size=(2, 2)))
        cnnDict['l1_conv1'].append(lasagne.layers.BatchNormLayer(
                cnnDict['l1_conv1'][-1],
                epsilon=epsilon,
                alpha=alpha))
        cnnDict['l1_conv1'].append(lasagne.layers.NonlinearityLayer(
                cnnDict['l1_conv1'][-1],
                nonlinearity=activation))

        # conv 2
        cnnDict['l2_conv2'] = []
        cnnDict['l2_conv2'].append(lasagne.layers.Conv2DLayer(
                cnnDict['l1_conv1'][-1],
                num_filters=256,
                filter_size=(3, 3),
                stride=(2, 2),
                pad=1,
                nonlinearity=lasagne.nonlinearities.identity))
        cnnDict['l2_conv2'].append(lasagne.layers.MaxPool2DLayer(cnnDict['l2_conv2'][-1], pool_size=(2, 2)))
        cnnDict['l2_conv2'].append(lasagne.layers.BatchNormLayer(
                cnnDict['l2_conv2'][-1],
                epsilon=epsilon,
                alpha=alpha))
        cnnDict['l2_conv2'].append(lasagne.layers.NonlinearityLayer(
                cnnDict['l2_conv2'][-1],
                nonlinearity=activation))

        # conv3
        cnnDict['l3_conv3'] = []
        cnnDict['l3_conv3'].append(lasagne.layers.Conv2DLayer(
                cnnDict['l2_conv2'][-1],
                num_filters=512,
                filter_size=(3, 3),
                pad=1,
                nonlinearity=lasagne.nonlinearities.identity))
        cnnDict['l3_conv3'].append(lasagne.layers.NonlinearityLayer(
                cnnDict['l3_conv3'][-1],
                nonlinearity=activation))

        # conv 4
        cnnDict['l4_conv4'] = []
        cnnDict['l4_conv4'].append(lasagne.layers.Conv2DLayer(
                cnnDict['l3_conv3'][-1],
                num_filters=512,
                filter_size=(3, 3),
                pad=1,
                nonlinearity=lasagne.nonlinearities.identity))
        cnnDict['l4_conv4'].append(lasagne.layers.NonlinearityLayer(
                cnnDict['l4_conv4'][-1],
                nonlinearity=activation))

        # conv 5
        cnnDict['l5_conv5'] = []
        cnnDict['l5_conv5'].append(lasagne.layers.Conv2DLayer(
                cnnDict['l4_conv4'][-1],
                num_filters=512,
                filter_size=(3, 3),
                pad=1,
                nonlinearity=lasagne.nonlinearities.identity))
        cnnDict['l5_conv5'].append(lasagne.layers.MaxPool2DLayer(
                cnnDict['l5_conv5'][-1],
                pool_size=(2, 2)))
        cnnDict['l5_conv5'].append(lasagne.layers.NonlinearityLayer(
                cnnDict['l5_conv5'][-1],
                nonlinearity=activation))

        # now we have output shape (nbValidFrames, 512,7,7) -> Flatten it.
        batch_size = cnnDict['l0_in'].input_var.shape[0]
        cnnDict['l6_reshape'] = L.ReshapeLayer(cnnDict['l5_conv5'][-1], (batch_size, 25088))

        # # conv 6
        # cnnDict['l6_conv6'] = []
        # cnnDict['l6_conv6'].append(lasagne.layers.Conv2DLayer(
        #         cnnDict['l5_conv5'][-1],
        #         num_filters=128,
        #         filter_size=(3, 3),
        #         pad=1,
        #         nonlinearity=lasagne.nonlinearities.identity))
        # cnnDict['l6_conv6'].append(lasagne.layers.MaxPool2DLayer(
        #         cnnDict['l6_conv6'][-1],
        #         pool_size=(2, 2)))
        # cnnDict['l6_conv6'].append(lasagne.layers.NonlinearityLayer(
        #         cnnDict['l6_conv6'][-1],
        #         nonlinearity=activation))

        # # this will output shape (nbValidFrames, 512,7,7). Flatten it.
        # batch_size = cnnDict['l0_in'].input_var.shape[0]
        # cnnDict['l6_reshape'] = L.ReshapeLayer(cnnDict['l6_conv6'][-1], (batch_size, 25088))

        # disable this layer for normal phoneme recognition
        # FC layer
        # cnnDict['l6_fc'] = []
        # cnnDict['l6_fc'].append(lasagne.layers.DenseLayer(
        #         cnnDict['l5_conv5'][-1],
        #        nonlinearity=lasagne.nonlinearities.identity,
        #        num_units=256))
        #
        # cnnDict['l6_fc'].append(lasagne.layers.NonlinearityLayer(
        #         cnnDict['l6_fc'][-1],
        #         nonlinearity=activation))


        cnnDict['l7_out'] = lasagne.layers.DenseLayer(
                cnnDict['l5_conv5'][-1],
                nonlinearity=lasagne.nonlinearities.softmax,
                num_units=nbClasses)

        # cnn = lasagne.layers.BatchNormLayer(
        #       cnn,
        #       epsilon=epsilon,
        #       alpha=alpha)

        return cnnDict, cnnDict['l7_out'], cnnDict['l6_reshape']

    def build_google_binary_CNN(self, input=None, activation=T.nnet.relu, alpha=0.1, epsilon=1e-4):
        alpha = .1
        epsilon = 1e-4
        activation = binary_net.binary_tanh_unit
        binary = True
        stochastic = False
        H = 1.
        W_LR_scale = "Glorot"

# Resnet stuff


    def build_resnet50_CNN(self, input=None, activation=T.nnet.relu, alpha=0.1, epsilon=1e-4):
        input = self.CNN_input_var
        nbClasses = self.num_output_units

        from lasagne.layers import BatchNormLayer, Conv2DLayer as ConvLayer, DenseLayer, ElemwiseSumLayer, InputLayer, \
            NonlinearityLayer, Pool2DLayer as PoolLayer
        from lasagne.nonlinearities import rectify, softmax
        def build_simple_block(incoming_layer, names,
                               num_filters, filter_size, stride, pad,
                               use_bias=False, nonlin=rectify):
            """Creates stacked Lasagne layers ConvLayer -> BN -> (ReLu)

            Parameters:
            ----------
            incoming_layer : instance of Lasagne layer
                Parent layer

            names : list of string
                Names of the layers in block

            num_filters : int
                Number of filters in convolution layer

            filter_size : int
                Size of filters in convolution layer

            stride : int
                Stride of convolution layer

            pad : int
                Padding of convolution layer

            use_bias : bool
                Whether to use bias in conlovution layer

            nonlin : function
                Nonlinearity type of Nonlinearity layer

            Returns
            -------
            tuple: (net, last_layer_name)
                net : dict
                    Dictionary with stacked layers
                last_layer_name : string
                    Last layer name
            """
            net = []
            net.append((
                names[0],
                ConvLayer(incoming_layer, num_filters, filter_size, pad, stride,
                          flip_filters=False, nonlinearity=None) if use_bias
                else ConvLayer(incoming_layer, num_filters, filter_size, stride, pad, b=None,
                               flip_filters=False, nonlinearity=None)
            ))

            net.append((
                names[1],
                BatchNormLayer(net[-1][1])
            ))
            if nonlin is not None:
                net.append((
                    names[2],
                    NonlinearityLayer(net[-1][1], nonlinearity=nonlin)
                ))

            return dict(net), net[-1][0]

        def build_residual_block(incoming_layer, ratio_n_filter=1.0, ratio_size=1.0, has_left_branch=False,
                                 upscale_factor=4, ix=''):
            """Creates two-branch residual block

            Parameters:
            ----------
            incoming_layer : instance of Lasagne layer
                Parent layer

            ratio_n_filter : float
                Scale factor of filter bank at the input of residual block

            ratio_size : float
                Scale factor of filter size

            has_left_branch : bool
                if True, then left branch contains simple block

            upscale_factor : float
                Scale factor of filter bank at the output of residual block

            ix : int
                Id of residual block

            Returns
            -------
            tuple: (net, last_layer_name)
                net : dict
                    Dictionary with stacked layers
                last_layer_name : string
                    Last layer name
            """
            simple_block_name_pattern = ['res%s_branch%i%s', 'bn%s_branch%i%s', 'res%s_branch%i%s_relu']

            net = {}

            # right branch
            net_tmp, last_layer_name = build_simple_block(
                    incoming_layer, map(lambda s: s % (ix, 2, 'a'), simple_block_name_pattern),
                    int(lasagne.layers.get_output_shape(incoming_layer)[1] * ratio_n_filter), 1, int(1.0 / ratio_size),
                    0)
            net.update(net_tmp)

            net_tmp, last_layer_name = build_simple_block(
                    net[last_layer_name], map(lambda s: s % (ix, 2, 'b'), simple_block_name_pattern),
                    lasagne.layers.get_output_shape(net[last_layer_name])[1], 3, 1, 1)
            net.update(net_tmp)

            net_tmp, last_layer_name = build_simple_block(
                    net[last_layer_name], map(lambda s: s % (ix, 2, 'c'), simple_block_name_pattern),
                    lasagne.layers.get_output_shape(net[last_layer_name])[1] * upscale_factor, 1, 1, 0,
                    nonlin=None)
            net.update(net_tmp)

            right_tail = net[last_layer_name]
            left_tail = incoming_layer

            # left branch
            if has_left_branch:
                net_tmp, last_layer_name = build_simple_block(
                        incoming_layer, map(lambda s: s % (ix, 1, ''), simple_block_name_pattern),
                        int(lasagne.layers.get_output_shape(incoming_layer)[1] * 4 * ratio_n_filter), 1,
                        int(1.0 / ratio_size),
                        0,
                        nonlin=None)
                net.update(net_tmp)
                left_tail = net[last_layer_name]

            net['res%s' % ix] = ElemwiseSumLayer([left_tail, right_tail], coeffs=1)
            net['res%s_relu' % ix] = NonlinearityLayer(net['res%s' % ix], nonlinearity=rectify)

            return net, 'res%s_relu' % ix

        net = {}
        net['input'] = InputLayer(shape=(None, 1, 120, 120), input_var=input)
        sub_net, parent_layer_name = build_simple_block(
                net['input'], ['conv1', 'bn_conv1', 'conv1_relu'],
                64, 7, 3, 2, use_bias=True)
        net.update(sub_net)
        net['pool1'] = PoolLayer(net[parent_layer_name], pool_size=3, stride=2, pad=0, mode='max', ignore_border=False)
        block_size = list('abc')
        parent_layer_name = 'pool1'
        for c in block_size:
            if c == 'a':
                sub_net, parent_layer_name = build_residual_block(net[parent_layer_name], 1, 1, True, 4, ix='2%s' % c)
            else:
                sub_net, parent_layer_name = build_residual_block(net[parent_layer_name], 1.0 / 4, 1, False, 4,
                                                                  ix='2%s' % c)
            net.update(sub_net)

        block_size = list('abcd')
        for c in block_size:
            if c == 'a':
                sub_net, parent_layer_name = build_residual_block(
                        net[parent_layer_name], 1.0 / 2, 1.0 / 2, True, 4, ix='3%s' % c)
            else:
                sub_net, parent_layer_name = build_residual_block(net[parent_layer_name], 1.0 / 4, 1, False, 4,
                                                                  ix='3%s' % c)
            net.update(sub_net)

        block_size = list('abcdef')
        for c in block_size:
            if c == 'a':
                sub_net, parent_layer_name = build_residual_block(
                        net[parent_layer_name], 1.0 / 2, 1.0 / 2, True, 4, ix='4%s' % c)
            else:
                sub_net, parent_layer_name = build_residual_block(net[parent_layer_name], 1.0 / 4, 1, False, 4,
                                                                  ix='4%s' % c)
            net.update(sub_net)

        block_size = list('abc')
        for c in block_size:
            if c == 'a':
                sub_net, parent_layer_name = build_residual_block(
                        net[parent_layer_name], 1.0 / 2, 1.0 / 2, True, 4, ix='5%s' % c)
            else:
                sub_net, parent_layer_name = build_residual_block(net[parent_layer_name], 1.0 / 4, 1, False, 4,
                                                                  ix='5%s' % c)
            net.update(sub_net)
        net['pool5'] = PoolLayer(net[parent_layer_name], pool_size=7, stride=1, pad=0,
                                 mode='average_exc_pad', ignore_border=False)
        net['fc1000'] = DenseLayer(net['pool5'], num_units=nbClasses,
                                   nonlinearity=None)  # number output units = nbClasses (global variable)
        net['prob'] = NonlinearityLayer(net['fc1000'], nonlinearity=softmax)

        # now we have output shape (nbValidFrames, 2048,1,1) -> Flatten it.
        batch_size = net['input'].input_var.shape[0]
        cnn_reshape = L.ReshapeLayer(net['pool5'], (batch_size, 2048))

        return net, net['prob'], cnn_reshape

    def build_cifar10_CNN_v2(self, input=None, nbClasses=39):
        from lasagne.layers import BatchNormLayer, Conv2DLayer as ConvLayer, DenseLayer, ElemwiseSumLayer, InputLayer, \
            NonlinearityLayer, Pool2DLayer as PoolLayer, DropoutLayer
        from lasagne.nonlinearities import rectify, softmax

        input = self.CNN_input_var
        nbClasses = self.num_output_units

        net = {}
        net['input'] = InputLayer((None, 1, 120, 120), input_var=input)
        net['conv1'] = ConvLayer(net['input'],
                                 num_filters=192,
                                 filter_size=5,
                                 pad=2,
                                 flip_filters=False)
        net['cccp1'] = ConvLayer(
                net['conv1'], num_filters=160, filter_size=1, flip_filters=False)
        net['cccp2'] = ConvLayer(
                net['cccp1'], num_filters=96, filter_size=1, flip_filters=False)
        net['pool1'] = PoolLayer(net['cccp2'],
                                 pool_size=3,
                                 stride=2,
                                 mode='max',
                                 ignore_border=False)
        net['drop3'] = DropoutLayer(net['pool1'], p=0.5)
        net['conv2'] = ConvLayer(net['drop3'],
                                 num_filters=192,
                                 filter_size=5,
                                 pad=2,
                                 flip_filters=False)
        net['cccp3'] = ConvLayer(
                net['conv2'], num_filters=192, filter_size=1, flip_filters=False)
        net['cccp4'] = ConvLayer(
                net['cccp3'], num_filters=192, filter_size=1, flip_filters=False)
        net['pool2'] = PoolLayer(net['cccp4'],
                                 pool_size=3,
                                 stride=2,
                                 mode='average_exc_pad',
                                 ignore_border=False)
        net['drop6'] = DropoutLayer(net['pool2'], p=0.5)
        net['conv3'] = ConvLayer(net['drop6'],
                                 num_filters=192,
                                 filter_size=3,
                                 pad=1,
                                 flip_filters=False)
        net['cccp5'] = ConvLayer(
                net['conv3'], num_filters=192, filter_size=1, flip_filters=False)
        net['cccp6'] = ConvLayer(
                net['cccp5'], num_filters=10, filter_size=1, flip_filters=False)
        net['pool3'] = PoolLayer(net['cccp6'],
                                 pool_size=8,
                                 mode='average_exc_pad',
                                 ignore_border=False)
        # net['output'] = FlattenLayer(net['pool3'])

        # now we have output shape (nbValidFrames, 10,4,4) -> Flatten it.
        batch_size = net['input'].input_var.shape[0]
        cnn_reshape = L.ReshapeLayer(net['pool3'], (batch_size, 160))


        net['dense1'] = lasagne.layers.DenseLayer(
                net['pool3'],
                nonlinearity=lasagne.nonlinearities.identity,
                num_units=1024)

        net['output'] = lasagne.layers.DenseLayer(
                net['dense1'],
                nonlinearity=lasagne.nonlinearities.softmax,
                num_units=nbClasses)

        return net, net['output'], cnn_reshape

    def build_cifar10_CNN(self, input=None, activation=T.nnet.relu, alpha=0.1, epsilon=1e-4):
        input = self.CNN_input_var
        nbClasses = self.num_output_units

        cnn_in = lasagne.layers.InputLayer(
                shape=(None, 1, 120, 120),
                input_var=input)

        # 128C3-128C3-P2
        cnn = lasagne.layers.Conv2DLayer(
                cnn_in,
                num_filters=128,
                filter_size=(3, 3),
                pad=1,
                nonlinearity=lasagne.nonlinearities.identity)

        cnn = lasagne.layers.BatchNormLayer(
                cnn,
                epsilon=epsilon,
                alpha=alpha)

        cnn = lasagne.layers.NonlinearityLayer(
                cnn,
                nonlinearity=activation)

        cnn = lasagne.layers.Conv2DLayer(
                cnn,
                num_filters=128,
                filter_size=(3, 3),
                pad=1,
                nonlinearity=lasagne.nonlinearities.identity)

        cnn = lasagne.layers.MaxPool2DLayer(cnn, pool_size=(2, 2))

        cnn = lasagne.layers.BatchNormLayer(
                cnn,
                epsilon=epsilon,
                alpha=alpha)

        cnn = lasagne.layers.NonlinearityLayer(
                cnn,
                nonlinearity=activation)

        # 256C3-256C3-P2
        cnn = lasagne.layers.Conv2DLayer(
                cnn,
                num_filters=256,
                filter_size=(3, 3),
                pad=1,
                nonlinearity=lasagne.nonlinearities.identity)

        cnn = lasagne.layers.BatchNormLayer(
                cnn,
                epsilon=epsilon,
                alpha=alpha)

        cnn = lasagne.layers.NonlinearityLayer(
                cnn,
                nonlinearity=activation)

        cnn = lasagne.layers.Conv2DLayer(
                cnn,
                num_filters=256,
                filter_size=(3, 3),
                pad=1,
                nonlinearity=lasagne.nonlinearities.identity)

        cnn = lasagne.layers.MaxPool2DLayer(cnn, pool_size=(2, 2))
        #
        cnn = lasagne.layers.BatchNormLayer(
                cnn,
                epsilon=epsilon,
                alpha=alpha)

        cnn = lasagne.layers.NonlinearityLayer(
                cnn,
                nonlinearity=activation)
        #
        # 512C3-512C3-P2
        cnn = lasagne.layers.Conv2DLayer(
                cnn,
                num_filters=512,
                filter_size=(3, 3),
                pad=1,
                nonlinearity=lasagne.nonlinearities.identity)

        cnn = lasagne.layers.BatchNormLayer(
                cnn,
                epsilon=epsilon,
                alpha=alpha)

        cnn = lasagne.layers.NonlinearityLayer(
                cnn,
                nonlinearity=activation)
        #
        cnn = lasagne.layers.Conv2DLayer(
                cnn,
                num_filters=512,
                filter_size=(3, 3),
                pad=1,
                nonlinearity=lasagne.nonlinearities.identity)

        cnn = lasagne.layers.MaxPool2DLayer(cnn, pool_size=(2, 2))

        cnn = lasagne.layers.BatchNormLayer(
                cnn,
                epsilon=epsilon,
                alpha=alpha)

        cnn = lasagne.layers.NonlinearityLayer(
                cnn,
                nonlinearity=activation)

        # print(cnn.output_shape)
        # now we have output shape (nbValidFrames, 512,15,15) -> Flatten it.
        batch_size = cnn_in.input_var.shape[0]
        cnn_reshape = L.ReshapeLayer(cnn, (batch_size, 115200))

        cnn = lasagne.layers.DenseLayer(
                cnn,
                nonlinearity=lasagne.nonlinearities.identity,
                num_units=256)
        #
        cnn = lasagne.layers.BatchNormLayer(
                cnn,
                epsilon=epsilon,
                alpha=alpha)

        cnn = lasagne.layers.NonlinearityLayer(
                cnn,
                nonlinearity=activation)

        cnn = lasagne.layers.DenseLayer(
                cnn,
                nonlinearity=lasagne.nonlinearities.softmax,
                num_units=nbClasses)

        return {}, cnn, cnn_reshape


    def build_lipreadingRNN(self, input, n_hidden_list=(100,), bidirectional=False, debug=False, logger=logger_combinedtools):
        net = {}
        #CNN output: (time_seq, features)
        # LSTM need (batch_size, time_seq, features). Batch_size = # videos processed in parallel = 1
        nbFeatures = input.output_shape[1]
        net['l1_in']= L.ReshapeLayer(input, (1, -1, nbFeatures))# 39 or 25088  (with dense softmax or direct conv outputs)

        if debug:
            n_batch, n_time_steps, n_features = net['l1_in'].output_shape
            logger.debug("  n_batch: %s | n_time_steps: %s | n_features: %s", n_batch, n_time_steps,    n_features)

        ## LSTM parameters
        # All gates have initializers for the input-to-gate and hidden state-to-gate
        # weight matrices, the cell-to-gate weight vector, the bias vector, and the nonlinearity.
        # The convention is that gates use the standard sigmoid nonlinearity,
        # which is the default for the Gate class.
        gate_parameters = L.recurrent.Gate(
                W_in=lasagne.init.Orthogonal(), W_hid=lasagne.init.Orthogonal(),
                b=lasagne.init.Constant(0.))
        cell_parameters = L.recurrent.Gate(
                W_in=lasagne.init.Orthogonal(), W_hid=lasagne.init.Orthogonal(),
                # Setting W_cell to None denotes that no cell connection will be used.
                W_cell=None, b=lasagne.init.Constant(0.),
                # By convention, the cell nonlinearity is tanh in an LSTM.
                nonlinearity=lasagne.nonlinearities.tanh)

        # generate layers of stacked LSTMs, possibly bidirectional

        net['l2_lstm'] = []
        for i in range(len(n_hidden_list)):
            n_hidden = n_hidden_list[i]

            if i == 0:
                input = net['l1_in']
            else:
                input = net['l2_lstm'][i - 1]

            nextForwardLSTMLayer = L.recurrent.LSTMLayer(
                    incoming=input, num_units=n_hidden,
                    # Here, we supply the gate parameters for each gate
                    ingate=gate_parameters, forgetgate=gate_parameters,
                    cell=cell_parameters, outgate=gate_parameters,
                    # We'll learn the initialization and use gradient clipping
                    learn_init=True, grad_clipping=100.)
            net['l2_lstm'].append(nextForwardLSTMLayer)

            if bidirectional:
                input = net['l2_lstm'][-1]
                # Use backward LSTM
                # The "backwards" layer is the same as the first,
                # except that the backwards argument is set to True.
                nextBackwardLSTMLayer = L.recurrent.LSTMLayer(
                        input, n_hidden, ingate=gate_parameters,
                        forgetgate=gate_parameters,
                        cell=cell_parameters, outgate=gate_parameters,
                        learn_init=True, grad_clipping=100., backwards=True)
                net['l2_lstm'].append(nextBackwardLSTMLayer)

                # The output of l_sum will be of shape (n_batch, max_n_time_steps, n_features)
                net['l2_lstm'].append(L.ElemwiseSumLayer([net['l2_lstm'][-2], net['l2_lstm'][-1]]))

        # we need to convert (batch_size, seq_length, num_features) to (batch_size * seq_length, num_features) because Dense networks can't deal with 2 unknown sizes
        net['l3_reshape'] = L.ReshapeLayer(net['l2_lstm'][-1], (-1, n_hidden_list[-1]))

        # print(L.count_params(net['l1_in']))
        # lstmParams = L.count_params(net['l2_lstm']) - L.count_params(net['l1_in'])
        # print(lstmParams)
        # if lstmParams > 6000000:
        #     print([L.count_params(net['l2_lstm'][i]) - L.count_params(net['l2_lstm'][i - 1]) for i in range(1, len(net['l2_lstm']))])
        #     print([L.count_params(net['l2_lstm'][i]) - L.count_params(net['l2_lstm'][i - 1]) for i in
        #      range(1, len(net['l2_lstm']))])
        #     import pdb;pdb.set_trace()
        if debug:
            self.print_RNN_network_structure(net)
        return net, net['l3_reshape']  #output shape: (nbFrames, nbHiddenLSTMunits)

    def build_softmax(self, inputLayer, nbClasses=39):

        softmaxLayer = lasagne.layers.DenseLayer(
                inputLayer,
                nonlinearity=lasagne.nonlinearities.softmax,
                num_units=nbClasses)

        return softmaxLayer


    def build_combined(self, lipreading_lout, audio_lout, dense_hidden_list, debug=False):

        # (we process one video at a time)
        # CNN_lout and RNN_lout should be shaped (batch_size, nbFeatures) with batch_size = nb_valid_frames in this video
        # for CNN_lout: nbFeatures = 512x7x7 = 25.088
        # for RNN_lout: nbFeatures = nbUnits(last LSTM layer)
        combinedNet = {}
        combinedNet['l_concat'] = L.ConcatLayer([lipreading_lout, audio_lout], axis=1)

        if debug:
            logger_combinedtools.debug("CNN output shape: %s", lipreading_lout.output_shape)
            logger_combinedtools.debug("RNN output shape: %s", audio_lout.output_shape)
            import pdb;pdb.set_trace()

        combinedNet['l_dense'] = []
        for i in range(len(dense_hidden_list)):
            n_hidden = dense_hidden_list[i]

            if i == 0:
                input = combinedNet['l_concat']
            else:
                input = combinedNet['l_dense'][i - 1]

            nextDenseLayer = L.DenseLayer(input,
                                          nonlinearity=lasagne.nonlinearities.rectify,
                                          num_units=n_hidden)
            #nextDenseLayer = L.DropoutLayer(nextDenseLayer, p=0.3)# TODO does dropout work?
            combinedNet['l_dense'].append(nextDenseLayer)

        # final softmax layer
        if len(combinedNet['l_dense']) == 0:  #if no hidden layers
            combinedNet['l_out'] = L.DenseLayer(combinedNet['l_concat'], num_units=self.num_output_units,
                                            nonlinearity=lasagne.nonlinearities.softmax)
        else:
            combinedNet['l_out'] = L.DenseLayer(combinedNet['l_dense'][-1], num_units=self.num_output_units,
                                            nonlinearity=lasagne.nonlinearities.softmax)

        return combinedNet, combinedNet['l_out']

    def print_RNN_network_structure(self, net=None, logger=logger_combinedtools):
        if net == None: net = self.audioNet_dict

        logger.debug("\n PRINTING Audio RNN network: \n %s ", sorted(net.keys()))
        for key in sorted(net.keys()):
            if 'lstm' in key:
                for layer in net['l2_lstm']:
                    try:
                        logger.debug(' %12s | in: %s | out: %s', key, layer.input_shape, layer.output_shape)
                    except:
                        logger.debug(' %12s | out: %s', key, layer.output_shape)
            else:
                try:
                    logger.debug(' %12s | in: %s | out: %s', key, net[key].input_shape, net[key].output_shape)
                except:
                    logger.debug(' %12s | out: %s', key, net[key].output_shape)
        return 0

    def print_CNN_network_structure(self, net=None, logger=logger_combinedtools):
        if net == None:
            cnnDict = self.CNN_dict
        else:
            cnnDict = net

        print("\n PRINTING image CNN structure: \n %s " % (sorted(cnnDict.keys())))
        for key in sorted(cnnDict.keys()):
            print(key)
            if 'conv' in key and type(cnnDict[key]) == list:
                for layer in cnnDict[key]:
                    try:
                        print('  %12s \nin: %s | out: %s' % (layer, layer.input_shape, layer.output_shape))
                    except:
                        print('  %12s \nout: %s' % (layer, layer.output_shape))
            else:
                try:
                    print('   %12s \nin: %s | out: %s' % (
                        cnnDict[key], cnnDict[key].input_shape, cnnDict[key].output_shape))
                except:
                    print('  %12s \nout: %s' % (cnnDict[key], cnnDict[key].output_shape))
        return 0

    def getParamsInfo(self):
        # print number of parameters
        nb_CNN_features = lasagne.layers.count_params(self.CNN_lout_features)
        nb_CNN = lasagne.layers.count_params(self.CNN_lout)
        nb_lipreading_features = lasagne.layers.count_params(self.lipreading_lout_features)
        nb_lipreading = L.count_params(self.lipreading_lout)
        nb_audio_features = lasagne.layers.count_params(self.audioNet_lout_features)
        nb_audio = lasagne.layers.count_params(self.audioNet_lout)
        nb_total = lasagne.layers.count_params(self.combined_lout)

        if self.lipreadingType == 'CNN_LSTM':  #features is then the output of the LSTM on top of CNN, so contains all the lipreading params
            if self.cnn_features == 'conv':
                nb_CNN_used = nb_CNN_features
            else: nb_CNN_used = nb_CNN
            nb_lipRNN = nb_lipreading - nb_CNN_used
        else:
            nb_CNN_used = nb_lipreading_features
            nb_lipRNN = 0

        nb_combining = nb_total - nb_lipreading_features - nb_audio

        nb_params = {}
        nb_params['nb_lipreading'] = nb_lipreading
        nb_params['nb_audio'] = nb_audio
        nb_params['nb_total'] = nb_total
        nb_params['nb_audio_features'] = nb_audio_features
        nb_params['nb_lipreading_features'] = nb_lipreading_features
        nb_params['nb_CNN_used'] = nb_CNN_used
        nb_params['nb_lipRNN'] = nb_lipRNN
        nb_params['nb_combining'] = nb_combining

        return nb_params

    # return True if successful load, false otherwise
    def load_model(self, model_type, roundParams=False, logger=logger_combinedtools):
        if not os.path.exists(self.model_paths[model_type]):
            logger.warning("WARNING: Loading %s Failed. \n path: %s", model_type, self.model_paths[model_type])
            return False

        # restore network weights
        with np.load(self.model_paths[model_type]) as f:
            param_values = [f['arr_%d' % i] for i in range(len(f.files))]
            if len(param_values) == 1: param_values = param_values[0]
            if model_type == 'audio':
                lout = self.audioNet_lout
            elif model_type == 'CNN':
                lout = self.CNN_lout
            elif model_type == 'CNN_LSTM':
                lout = self.lipreading_lout
            elif model_type == 'combined':
                lout = self.combined_lout
            else:
                logger.error('Wrong network type. No weights loaded')#.format(model_type))
                return False
            try:
                if roundParams: lasagne.layers.set_all_param_values(lout, self.round_params(param_values))
                else:
                    #print(len(param_values));import pdb;pdb.set_trace();
                    lasagne.layers.set_all_param_values(lout, param_values)

            except:
                try:
                    if roundParams: lasagne.layers.set_all_param_values(lout, self.round_params(*param_values))
                    else: lasagne.layers.set_all_param_values(lout, *param_values)
                except:
                    logger.warning('Warning: %s', traceback.format_exc())  # , model_path)
                    import pdb;pdb.set_trace()

        logger.info("Loading %s parameters successful.", model_type)
        return True

    def round_params(self, param_values):
        for i in range(len(param_values)):
            param_values[i] = param_values[i].astype(np.float16)
            param_values[i] = param_values[i].astype(np.float32)

        return param_values

    # set as many network parameters as possible by hierarchical loading of subnetworks
    # eg for combined: if no traied combined network, try to load subnets of audio and lipreading
    def setNetworkParams(self, runType, overwriteSubnets=False, roundParams=False, logger=logger_combinedtools):
        if runType == 'combined':
            logger.info("\nAttempting to load combined model: %s", self.model_paths['combined'])

            success = self.load_model(model_type='combined', roundParams=roundParams)
            if (not success) or overwriteSubnets:
                logger.warning("No complete combined network found, loading parts...")

                logger.info("CNN : %s", self.model_paths['CNN'])
                self.load_model(model_type='CNN', roundParams=roundParams)

                if self.lipreadingType == 'CNN_LSTM':  # LIP_RNN_HIDDEN_LIST != None:
                    logger.info("CNN_LSTM : %s", self.model_paths['CNN_LSTM'])
                    self.load_model(model_type='CNN_LSTM', roundParams=roundParams)

                logger.info("Audio : %s", self.model_paths['audio'])
                self.load_model(model_type='audio', roundParams=roundParams)

        elif runType == 'lipreading':

            if self.lipreadingType == 'CNN_LSTM':
                logger.info("\nAttempting to load lipreading CNN_LSTM model: %s",
                            self.model_paths['CNN_LSTM'])

                #try to load CNN_LSTM; if not works just load the CNN so you can train the LSTM based on that
                success = self.load_model(model_type='CNN_LSTM', roundParams=roundParams)
                if not success:
                    logger.warning("No complete CNN_LSTM network found, loading parts...")
                    self.load_model(model_type='CNN', roundParams=roundParams)
            else:
                logger.info("\nAttempting to load lipreading CNN model: %s",   self.model_paths['CNN'])
                success = self.load_model(model_type='CNN', roundParams=roundParams)

        else: ## runType == 'audio':
            logger.info("\nAttempting to load audio model: %s",
                        self.model_paths['audio'])
            success = self.load_model(model_type='audio', roundParams=roundParams)
        return success

    def save_model(self, model_name, logger=logger_combinedtools):
        if not os.path.exists(os.path.dirname(model_name)):
            os.makedirs(os.path.dirname(model_name))
        np.savez(model_name + '.npz', self.best_param)

    def build_functions(self, runType, train=False, allowSubnetTraining=False, debug=False, logger=logger_combinedtools):

        k = 3;  # top k accuracy
        ##########################
        ## For Lipreading part  ##
        ##########################
        if runType == 'lipreading':
            # Targets are 2D for the LSTM, but needs only 1D for the CNN -> need to flatten everywhere
            #import pdb;pdb.set_trace()
            # For information: only CNN classification, with softmax to 39 phonemes
            CNN_test_network_output = L.get_output(self.CNN_lout, deterministic=True)
            CNN_test_loss = LO.categorical_crossentropy(CNN_test_network_output, self.targets_var.flatten());
            CNN_test_loss = CNN_test_loss.mean()
            CNN_test_acc = T.mean(T.eq(T.argmax(CNN_test_network_output, axis=1), self.targets_var.flatten()),
                              dtype=theano.config.floatX)
            CNN_top3_acc = T.mean(lasagne.objectives.categorical_accuracy(CNN_test_network_output, self.targets_var.flatten(), top_k=k))
            self.CNN_val_fn = theano.function([self.CNN_input_var, self.targets_var], [CNN_test_loss,
                                                                         CNN_test_acc,
                                                                         CNN_top3_acc])


            # The whole lipreading network (different if CNN-LSTM architecture, otherwise same as CNN-softmax)
            # for validation: disable dropout etc layers -> deterministic
            lipreading_test_network_output = L.get_output(self.lipreading_lout, deterministic=True)
            lipreading_preds = T.argmax(lipreading_test_network_output, axis=1) #prediction with maximum probability
            #self.lipreading_predictions_fn = theano.function([self.CNN_input_var], lipreading_preds)

            lipreading_test_acc = T.mean(T.eq(T.argmax(lipreading_test_network_output, axis=1), self.targets_var.flatten()),
                              dtype=theano.config.floatX)
            lipreading_test_loss = LO.categorical_crossentropy(lipreading_test_network_output, self.targets_var.flatten());
            lipreading_test_loss = lipreading_test_loss.mean()

            # Top k accuracy
            lipreading_top3_acc = T.mean(lasagne.objectives.categorical_accuracy(lipreading_test_network_output,
                                                                                 self.targets_var.flatten(), top_k=k))
            self.lipreading_top3acc_fn = theano.function([self.CNN_input_var, self.targets_var], lipreading_top3_acc)

            self.lipreading_val_fn = theano.function([self.CNN_input_var, self.targets_var], [lipreading_test_loss,
                                                                         lipreading_test_acc,
                                                                         lipreading_top3_acc])
            self.lipreading_val_preds_fn = theano.function([self.CNN_input_var, self.targets_var],
                                                           [lipreading_test_loss,
                                                            lipreading_test_acc,
                                                            lipreading_top3_acc,
                                                            lipreading_preds])

            if debug:
                CNN_test_loss, CNN_test_acc, CNN_top3_acc = self.CNN_val_fn(self.images, self.validLabels)
                logger.debug("\n\nCNN network only: \ntest loss: %s \n test acc: %s \n top3_acc: %s",
                                        CNN_test_loss, CNN_test_acc*100.0, CNN_top3_acc*100.0)

                lipreading_test_loss, lipreading_test_acc, lipreading_top3_acc = self.lipreading_val_fn(self.images, self.validLabels)
                logger.debug("\n\n Lipreading network: \ntest loss: %s \n test acc: %s \n top3_acc: %s",
                             lipreading_test_loss, lipreading_test_acc * 100.0, lipreading_top3_acc * 100.0)


            # For training, use nondeterministic output
            lipreading_network_output = L.get_output(self.lipreading_lout, deterministic=False)
            self.lipreading_out_fn = theano.function([self.CNN_input_var], lipreading_network_output)

            # cross-entropy loss
            lipreading_loss_pointwise = LO.categorical_crossentropy(lipreading_network_output, self.targets_var.flatten());
            lipreading_loss = lasagne.objectives.aggregate(lipreading_loss_pointwise)
            # lipreading_loss = lipreading_loss_pointwise.mean()

            # set all params to trainable
            lipreading_params = L.get_all_params(self.lipreading_lout, trainable=True)

            if self.lipreadingType == 'CNN_LSTM': #only train the LSTM network, don't touch the CNN
                if not allowSubnetTraining:
                    lipreading_params = list(set(lipreading_params) - set(L.get_all_params(self.CNN_lout, trainable=True)))

            lipreading_updates = lasagne.updates.adam(loss_or_grads=lipreading_loss, params=lipreading_params, learning_rate=self.LR_var)
            # Compile a function performing a training step on a mini-batch (by giving the updates dictionary)
            # and returning the corresponding training loss:
            self.lipreading_train_fn = theano.function([self.CNN_input_var, self.targets_var, self.LR_var], lipreading_loss, updates=lipreading_updates)

            if debug:
                output = self.lipreading_out_fn(self.images)
                logger.debug(" lipreading output shape: %s", output.shape)
                import pdb;pdb.set_trace()
        ####################
        ## For Audio Part ##
        ####################
        if runType == 'audio':
            # LSTM in lasagne: see https://github.com/craffel/Lasagne-tutorial/blob/master/examples/recurrent.py
            # and also         http://colinraffel.com/talks/hammer2015recurrent.pdf

            if debug:
                logger.debug("\n\n Audio Network")
                self.print_RNN_network_structure()

            # using the lasagne SliceLayer
            audio_valid_network_output = L.get_output(self.audioNet_dict['l7_out_valid'])
            self.audio_valid_network_output_fn = theano.function(
                    [self.audio_inputs_var, self.audio_masks_var, self.audio_valid_frames_var], audio_valid_network_output)

            audio_valid_network_output_flattened = L.get_output(self.audioNet_lout_flattened)
            self.audio_network_output_flattened_fn = theano.function(
                    [self.audio_inputs_var, self.audio_masks_var, self.audio_valid_frames_var],
                    audio_valid_network_output_flattened)

            audio_valid_predictions = T.argmax(audio_valid_network_output_flattened, axis=1)  # TODO axis 1 or 2?
            self.audio_predictions_fn = theano.function(
                    [self.audio_inputs_var, self.audio_masks_var, self.audio_valid_frames_var],
                    audio_valid_predictions, name='valid_predictions_fn')

            # top k accuracy
            audio_top1_acc = T.mean(lasagne.objectives.categorical_accuracy(
                    audio_valid_network_output_flattened, self.targets_var.flatten(), top_k=1))
            self.audio_top1_acc_fn = theano.function(
                    [self.audio_inputs_var, self.audio_masks_var, self.audio_valid_frames_var,
                     self.targets_var], audio_top1_acc)
            audio_top3_acc = T.mean(lasagne.objectives.categorical_accuracy(
                    audio_valid_network_output_flattened, self.targets_var.flatten(), top_k=k))
            self.audio_top3_acc_fn = theano.function(
                    [self.audio_inputs_var, self.audio_masks_var, self.audio_valid_frames_var,
                     self.targets_var], audio_top3_acc)
            if debug:
                try:
                    valid_out = self.audio_valid_network_output_fn(self.mfccs, self.masks, self.validAudioFrames)
                    logger.debug('valid_out.shape:        %s', valid_out.shape)
                    # logger.debug('valid_out, value: \n%s', valid_out)

                    valid_out_flattened = self.audio_network_output_flattened_fn(self.mfccs, self.masks,
                                                                                 self.validAudioFrames)
                    logger.debug('valid_out_flat.shape:   %s', valid_out_flattened.shape)
                    # logger.debug('valid_out_flat, value: \n%s', valid_out_flattened)

                    valid_preds2 = self.audio_predictions_fn(self.mfccs, self.masks, self.validAudioFrames)
                    logger.debug('valid_preds2.shape:     %s', valid_preds2.shape)
                    # logger.debug('valid_preds2, value: \n%s', valid_preds2)

                    logger.debug('validAudioFrames.shape: %s', self.validAudioFrames.shape)
                    logger.debug('valid_targets.shape:    %s', self.validLabels.shape)
                    logger.debug('valid_targets, value:   %s', self.validLabels)

                    top1 = self.audio_top1_acc_fn(self.mfccs, self.masks, self.validAudioFrames, self.validLabels)
                    logger.debug("top 1 accuracy:   %s", top1 * 100.0)

                    top3 = self.audio_top3_acc_fn(self.mfccs, self.masks, self.validAudioFrames, self.validLabels)
                    logger.debug("top 3 accuracy:   %s", top3 * 100.0)

                except Exception as error:
                    print('caught this error: ' + traceback.format_exc());
                    import pdb;                    pdb.set_trace()

            # with Lasagne SliceLayer outputs:
            audio_cost_pointwise = lasagne.objectives.categorical_crossentropy(audio_valid_network_output_flattened,
                                                                         self.targets_var.flatten())
            audio_cost = lasagne.objectives.aggregate(audio_cost_pointwise)

            # Functions for computing cost and training
            self.audio_val_fn = theano.function(
                    [self.audio_inputs_var, self.audio_masks_var, self.audio_valid_frames_var, self.targets_var],
                    [audio_cost, audio_top1_acc, audio_top3_acc], name='validate_fn')
            self.audio_val_preds_fn = theano.function(
                    [self.audio_inputs_var, self.audio_masks_var, self.audio_valid_frames_var, self.targets_var],
                    [audio_cost, audio_top1_acc, audio_top3_acc, audio_valid_predictions], name='validate_fn')

            if debug:
                self.audio_cost_pointwise_fn = theano.function([self.audio_inputs_var, self.audio_masks_var,
                                                                self.audio_valid_frames_var, self.targets_var],
                                                               audio_cost_pointwise, name='cost_pointwise_fn')
                # logger.debug('cost pointwise: %s',
                #              self.audio_cost_pointwise_fn(self.mfccs, self.masks, self.validAudioFrames, self.validLabels))
                evaluate_cost = self.audio_val_fn(self.mfccs, self.masks, self.validAudioFrames, self.validLabels)
                logger.debug('cost:     {:.3f}'.format(float(evaluate_cost[0])))
                logger.debug('accuracy: {:.3f} %'.format(float(evaluate_cost[1]) * 100))
                logger.debug('Top 3 accuracy: {:.3f} %'.format(float(evaluate_cost[2]) * 100))

                # pdb.set_trace()

            # Retrieve all trainable parameters from the network
            audio_params = L.get_all_params(self.audioNet_lout, trainable=True)
            self.audio_updates = lasagne.updates.adam(loss_or_grads=audio_cost, params=audio_params, learning_rate=self.LR_var)
            self.audio_train_fn = theano.function([self.audio_inputs_var, self.audio_masks_var, self.audio_valid_frames_var,
                                                   self.targets_var, self.LR_var],
                                                  audio_cost, updates=self.audio_updates, name='train_fn')

        #######################
        ### For Combined part ##
        ########################
        if runType == 'combined':
            if debug:
                logger.debug("\n\n Combined Network")
                RNN_features = L.get_output(self.audioNet_lout_features)
                CNN_features = L.get_output(self.CNN_lout_features)
                get_features = theano.function([self.CNN_input_var, self.audio_inputs_var, self.audio_masks_var,
                                                self.audio_valid_frames_var], [RNN_features, CNN_features])
                try:
                    RNN_feat, CNN_feat = get_features(self.images,
                                                      self.mfccs,
                                                      self.masks,
                                                      self.validAudioFrames)
                    logger.debug("RNN_feat.shape: %s", RNN_feat.shape)
                    logger.debug("CNN_feat.shape: %s", CNN_feat.shape)

                except Exception as error:
                    print('caught this error: ' + traceback.format_exc());
                    import pdb;
                    pdb.set_trace()


            # For training, use nondeterministic output
            combined_network_output = L.get_output(self.combined_lout, deterministic=False)

            # cross-entropy loss
            combined_loss = LO.categorical_crossentropy(combined_network_output, self.targets_var.flatten())
            combined_loss = combined_loss.mean()
            # weight regularization
            weight_decay = 1e-5
            combined_weightsl2 = lasagne.regularization.regularize_network_params(self.combined_lout, lasagne.regularization.l2)
            combined_loss += weight_decay * combined_weightsl2

            # set all params to trainable
            combined_params = L.get_all_params(self.combined_lout, trainable=True)

            # remove subnet parameters so they are kept fixed (already pretrained)
            if not allowSubnetTraining:
                combined_params = list(set(combined_params) - set(L.get_all_params(self.CNN_lout, trainable=True)))
                combined_params = list(set(combined_params) - set(L.get_all_params(self.audioNet_lout, trainable=True)))

            combined_updates = lasagne.updates.adam(loss_or_grads=combined_loss, params=combined_params, learning_rate=self.LR_var)

            self.combined_train_fn = theano.function([self.CNN_input_var,self.audio_inputs_var, self.audio_masks_var,
                                                      self.audio_valid_frames_var,
                                                      self.targets_var, self.LR_var], combined_loss, updates=combined_updates)

            # for validation: disable dropout etc layers -> deterministic
            combined_test_network_output = L.get_output(self.combined_lout, deterministic=True)
            combined_preds = T.argmax(combined_test_network_output, axis=1)
            combined_test_acc = T.mean(T.eq(combined_preds, self.targets_var.flatten()),
                              dtype=theano.config.floatX)
            combined_test_loss = LO.categorical_crossentropy(combined_test_network_output, self.targets_var.flatten());
            combined_test_loss = combined_test_loss.mean()

            self.combined_output_fn = theano.function(
                    [self.CNN_input_var, self.audio_inputs_var, self.audio_masks_var, self.audio_valid_frames_var],
                    combined_test_network_output)

            combined_top3_acc = T.mean(lasagne.objectives.categorical_accuracy(combined_test_network_output,
                                                                               self.targets_var.flatten(), top_k=k))
            self.combined_top3acc_fn = theano.function([self.CNN_input_var, self.audio_inputs_var, self.audio_masks_var,
                                                        self.audio_valid_frames_var,
                                                        self.targets_var], combined_top3_acc)

            self.combined_val_fn = theano.function([self.CNN_input_var, self.audio_inputs_var, self.audio_masks_var,
                                                    self.audio_valid_frames_var,
                                                    self.targets_var], [combined_test_loss, combined_test_acc, combined_top3_acc])
            self.combined_val_preds_fn = theano.function([self.CNN_input_var, self.audio_inputs_var, self.audio_masks_var,
                                                    self.audio_valid_frames_var,
                                                    self.targets_var],
                                                   [combined_test_loss, combined_test_acc, combined_top3_acc, combined_preds])
            if debug:
                try:
                    comb_test_loss, comb_test_acc, comb_top3_acc = self.combined_val_fn(self.images,
                                                                                        self.mfccs,
                                                                                        self.masks,
                                                                                        self.validAudioFrames,
                                                                                        self.validLabels)
                    logger.debug("Combined network: \ntest loss: %s \n test acc: %s \n top3_acc: %s",
                                 comb_test_loss, comb_test_acc * 100.0, comb_top3_acc * 100.0)
                except Exception as error:
                    print('caught this error: ' + traceback.format_exc());
                    import pdb;
                    pdb.set_trace()


    def shuffle(self, lst):
        import random
        c = list(zip(*lst))
        random.shuffle(c)
        shuffled = zip(*c)
        for i in range(len(shuffled)):
            shuffled[i] = list(shuffled[i])
        return shuffled

    # This function trains the model a full epoch (on the whole dataset)
    def train_epoch(self, runType, images, mfccs, validLabels, valid_frames, LR, batch_size=-1):
        if batch_size == -1: batch_size = self.batch_size  # always 1

        cost = 0;
        nb_batches = len(mfccs) / batch_size

        if "volunteers" in self.test_dataset:
            loops = range(nb_batches)
        else: loops = tqdm(range(nb_batches), total=nb_batches)
        for i in loops:
            batch_images = images[i * batch_size:(i + 1) * batch_size][0]
            batch_mfccs = mfccs[i * batch_size:(i + 1) * batch_size]
            batch_validLabels = validLabels[i * batch_size:(i + 1) * batch_size]
            batch_valid_frames = valid_frames[i * batch_size:(i + 1) * batch_size]
            batch_masks = generate_masks(batch_mfccs, valid_frames=batch_valid_frames, batch_size=batch_size)
            # now pad inputs and target to maxLen
            batch_mfccs = pad_sequences_X(batch_mfccs)
            batch_valid_frames = pad_sequences_y(batch_valid_frames)
            batch_validLabels = pad_sequences_y(batch_validLabels)
            # print("batch_mfccs.shape: ", batch_mfccs.shape)
            # print("batch_validLabels.shape: ", batch_validLabels.shape)
            if runType == 'audio':
                cst = self.audio_train_fn(batch_mfccs, batch_masks, batch_valid_frames,
                                          batch_validLabels, LR)  # training
            elif runType == 'lipreading':
                cst = self.lipreading_train_fn(batch_images, batch_validLabels, LR)
            else:  # train combined
                cst = self.combined_train_fn(batch_images, batch_mfccs, batch_masks, batch_valid_frames,
                                             batch_validLabels, LR)
            cost += cst;

        return cost, nb_batches

    # This function trains the model a full epoch (on the whole dataset)
    def val_epoch(self, runType, images, mfccs, validLabels, valid_frames, batch_size=-1):
        if batch_size == -1: batch_size = self.batch_size

        cost = 0;
        accuracy = 0
        top3_accuracy = 0
        nb_batches = len(mfccs) / batch_size

        if "volunteers" in self.test_dataset: loops = range(nb_batches)
        else: loops = tqdm(range(nb_batches), total=nb_batches)
        for i in loops:
            batch_images = images[i * batch_size:(i + 1) * batch_size][0]
            batch_mfccs = mfccs[i * batch_size:(i + 1) * batch_size]
            batch_validLabels = validLabels[i * batch_size:(i + 1) * batch_size]
            batch_valid_frames = valid_frames[i * batch_size:(i + 1) * batch_size]
            batch_masks = generate_masks(batch_mfccs, valid_frames=batch_valid_frames, batch_size=batch_size)

            # now pad inputs and target to maxLen
            batch_mfccs = pad_sequences_X(batch_mfccs)
            batch_valid_frames = pad_sequences_y(batch_valid_frames)
            batch_validLabels = pad_sequences_y(batch_validLabels)

            # print("batch_mfccs.shape: ", batch_mfccs.shape)
            # print("batch_validLabels.shape: ", batch_validLabels.shape)
            # import pdb;    pdb.set_trace()

            if runType == 'audio':
                cst, acc, top3_acc = self.audio_val_fn(batch_mfccs, batch_masks, batch_valid_frames,
                                                       batch_validLabels)  # training
            elif runType == 'lipreading':
                cst, acc, top3_acc = self.lipreading_val_fn(batch_images, batch_validLabels)
            else:  # train combined
                cst, acc, top3_acc = self.combined_val_fn(batch_images, batch_mfccs, batch_masks, batch_valid_frames,
                                                          batch_validLabels)
            cost += cst;
            accuracy += acc
            top3_accuracy += top3_acc

        return cost, accuracy, top3_accuracy, nb_batches

    # This function trains the model a full epoch (on the whole dataset)
    def val_epoch_withPreds(self, runType, images, mfccs, validLabels, valid_frames, batch_size=-1):
        if batch_size == -1: batch_size = self.batch_size

        cost = 0;
        accuracy = 0
        top3_accuracy = 0
        nb_batches = len(mfccs) / batch_size
        predictions = []

        if "volunteers" in self.test_dataset:
            loops = range(nb_batches)
        else:
            loops = tqdm(range(nb_batches), total=nb_batches)
        for i in loops:
            batch_images = images[i * batch_size:(i + 1) * batch_size][0]
            batch_mfccs = mfccs[i * batch_size:(i + 1) * batch_size]
            batch_validLabels = validLabels[i * batch_size:(i + 1) * batch_size]
            batch_valid_frames = valid_frames[i * batch_size:(i + 1) * batch_size]
            batch_masks = generate_masks(batch_mfccs, valid_frames=batch_valid_frames, batch_size=batch_size)

            # now pad inputs and target to maxLen
            batch_mfccs = pad_sequences_X(batch_mfccs)
            batch_valid_frames = pad_sequences_y(batch_valid_frames)
            batch_validLabels = pad_sequences_y(batch_validLabels)

            # print("batch_mfccs.shape: ", batch_mfccs.shape)
            # print("batch_validLabels.shape: ", batch_validLabels.shape)
            # import pdb;    pdb.set_trace()

            if runType == 'audio':
                cst, acc, top3_acc, preds = self.audio_val_preds_fn(batch_mfccs, batch_masks, batch_valid_frames,
                                                       batch_validLabels)  # training
            elif runType == 'lipreading':
                cst, acc, top3_acc, preds = self.lipreading_val_preds_fn(batch_images, batch_validLabels)
            else:  # train combined
                cst, acc, top3_acc, preds = self.combined_val_preds_fn(batch_images, batch_mfccs, batch_masks,
                                                          batch_valid_frames,
                                                          batch_validLabels)
            cost += cst;
            accuracy += acc
            top3_accuracy += top3_acc
            predictions.append(list(preds))

        return cost, accuracy, top3_accuracy, nb_batches, predictions

    # evaluate many TRAINING speaker files -> train loss, val loss and val error. Load them in one by one (so they fit in memory)
    def evalTRAINING(self, trainingSpeakerFiles, LR, runType='audio', shuffleEnabled=True, sourceDataDir=None,
                     storeProcessed=False, processedDir=None,
                     withNoise=False, noiseType='white', ratio_dB=-3,
                     verbose=False, logger=logger_combinedtools):
        train_cost = 0;
        val_acc = 0;
        val_cost = 0;
        val_topk_acc = 0;
        nb_train_batches = 0;
        nb_val_batches = 0;

        # for each speaker, pass over the train set, then val set. (test is other files). save the results.
        for speakerFile in tqdm(trainingSpeakerFiles, total=len(trainingSpeakerFiles)):
            if verbose: logger.debug("processing %s", speakerFile)
            train, val, test = preprocessingCombined.getOneSpeaker(
                    speakerFile=speakerFile, sourceDataDir=sourceDataDir,
                    trainFraction=0.8, validFraction=0.2,
                    storeProcessed=storeProcessed, processedDir=processedDir, logger=logger,
                    withNoise=withNoise, noiseType=noiseType, ratio_dB=ratio_dB)

            if shuffleEnabled: train = self.shuffle(train)
            images_train, mfccs_train, audioLabels_train, validLabels_train, validAudioFrames_train = train
            images_val, mfccs_val, audioLabels_val, validLabels_val, validAudioFrames_val = val
            images_test, mfccs_test, audioLabels_test, validLabels_test, validAudioFrames_test = test

            if verbose:
                logger.debug("the number of training examples is: %s", len(images_train))
                logger.debug("the number of valid examples is:    %s", len(images_val))
                logger.debug("the number of test examples is:     %s", len(images_test))

            train_cost_one, train_batches_one = self.train_epoch(runType=runType,
                                                                 images=images_train,
                                                                 mfccs=mfccs_train,
                                                                 validLabels=validLabels_train,
                                                                 valid_frames=validAudioFrames_train,
                                                                 LR=LR)
            train_cost += train_cost_one;
            nb_train_batches += train_batches_one

            # get results for validation  set
            val_cost_one, val_acc_one, val_topk_acc_one, val_batches_one = self.val_epoch(runType=runType,
                                                                                          images=images_val,
                                                                                          mfccs=mfccs_val,
                                                                                          validLabels=validLabels_val,
                                                                                          valid_frames=validAudioFrames_val)
            val_cost += val_cost_one;
            val_acc += val_acc_one;
            val_topk_acc += val_topk_acc_one
            nb_val_batches += val_batches_one;

            if verbose:
                logger.debug("  this speaker results: ")
                logger.debug("\ttraining cost:     %s", train_cost_one / train_batches_one)
                logger.debug("\tvalidation cost:   %s", val_cost_one / val_batches_one)
                logger.debug("\vvalidation acc rate:  %s %%", val_acc_one / val_batches_one * 100)
                logger.debug("\vvalidation top 3 acc rate:  %s %%", val_topk_acc_one / val_batches_one * 100)

        # get the average over all speakers
        train_cost /= nb_train_batches
        val_cost /= nb_val_batches
        val_acc = val_acc / nb_val_batches * 100  # convert to %
        val_topk_acc = val_topk_acc / nb_val_batches * 100  # convert to %

        return train_cost, val_cost, val_acc, val_topk_acc

    def evalTEST(self, testSpeakerFiles, runType='audio', sourceDataDir=None, storeProcessed=False, processedDir=None,
                 withNoise=False, noiseType='white', ratio_dB=-3,
                 verbose=False, logger=logger_combinedtools):

        test_acc = 0;
        test_cost = 0;
        test_topk_acc = 0;
        nb_test_batches = 0;
        # for each speaker, pass over the train set, then test set. (test is other files). save the results.
        for speakerFile in tqdm(testSpeakerFiles, total=len(testSpeakerFiles)):
            if verbose: logger.debug("processing %s", speakerFile)
            train, val, test = preprocessingCombined.getOneSpeaker(
                    speakerFile=speakerFile, sourceDataDir=sourceDataDir,
                    trainFraction=0.0, validFraction=0.0,
                    storeProcessed=storeProcessed, processedDir=processedDir, logger=logger,
                    withNoise=False, noiseType='white', ratio_dB=-3)

            images_train, mfccs_train, audioLabels_train, validLabels_train, validAudioFrames_train = train
            images_val, mfccs_val, audioLabels_val, validLabels_val, validAudioFrames_val = val
            images_test, mfccs_test, audioLabels_test, validLabels_test, validAudioFrames_test = test

            if verbose:
                logger.debug("the number of training examples is: %s", len(images_train))
                logger.debug("the number of valid examples is:    %s", len(images_val))
                logger.debug("the number of test examples is:     %s", len(images_test))
                import pdb;pdb.set_trace()

            # get results for testidation  set
            test_cost_one, test_acc_one, test_topk_acc_one, test_batches_one = self.val_epoch(runType=runType,
                                                                                              images=images_test,
                                                                                              mfccs=mfccs_test,
                                                                                              validLabels=validLabels_test,
                                                                                              valid_frames=validAudioFrames_test)
            test_acc += test_acc_one;
            test_cost += test_cost_one;
            test_topk_acc += test_topk_acc_one
            nb_test_batches += test_batches_one;

            if verbose:
                logger.debug("  this speaker results: ")
                logger.debug("\ttest cost:   %s", test_cost_one / test_batches_one)
                logger.debug("\vtest acc rate:  %s %%", test_acc_one / test_batches_one * 100)
                logger.debug("\vtest  top 3 acc rate:  %s %%", test_topk_acc_one / test_batches_one * 100)

        # get the average over all speakers
        test_cost /= nb_test_batches
        test_acc = test_acc / nb_test_batches * 100
        test_topk_acc = test_topk_acc / nb_test_batches * 100

        return test_cost, test_acc, test_topk_acc

    def train(self, datasetFiles, database_binaryDir, runType='combined', storeProcessed=False, processedDir=None,
              save_name='Best_model', datasetName='TCDTIMIT', nbPhonemes=39, viseme=False,
              num_epochs=40, batch_size=1, LR_start=1e-4, LR_decay=1,
              justTest=False, withNoise=False, noiseType = 'white', ratio_dB = -3,
              shuffleEnabled=True, compute_confusion=False, debug=False, logger=logger_combinedtools):

        trainingSpeakerFiles, testSpeakerFiles = datasetFiles
        logger.info("\n* Starting training...")

        best_val_acc, test_acc = self.loadPreviousResults(save_name)

        logger.info("Initial best Val acc: %s", best_val_acc)
        logger.info("Initial best test acc: %s\n", test_acc)

        # init some performance keepers
        best_epoch = 1
        LR = LR_start

        self.epochsNotImproved = 0


        if not self.loadPerSpeaker:  #load all the lipspeakers in memory, then don't touch the files -> no reloading needed = faster training
            trainPath = os.path.expanduser("~/TCDTIMIT/combinedSR/TCDTIMIT/binaryLipspeakers/allLipspeakersTrain.pkl")
            valPath = os.path.expanduser("~/TCDTIMIT/combinedSR/TCDTIMIT/binaryLipspeakers/allLipspeakersVal.pkl")
            testPath = os.path.expanduser("~/TCDTIMIT/combinedSR/TCDTIMIT/binaryLipspeakers/allLipspeakersTest.pkl")
            if viseme: 
                trainPath = trainPath.replace(".pkl","_viseme.pkl")
                valPath = valPath.replace(".pkl", "_viseme.pkl")
                testPath = testPath.replace(".pkl", "_viseme.pkl")
            allImages_train, allMfccs_train, allAudioLabels_train, allValidLabels_train, allValidAudioFrames_train = unpickle(trainPath)
            allImages_val, allMfccs_val, allAudioLabels_val, allValidLabels_val, allValidAudioFrames_val = unpickle(valPath)
            allImages_test, allMfccs_test, allAudioLabels_test, allValidLabels_test, allValidAudioFrames_test = unpickle(testPath)

            # if you wish to train with noise, you need to replace the audio data with noisy audio from audioSR/firDataset/audioToPkl_perVideo.py,
            # like so (but also for train and val)
            if withNoise:
                testPath = os.path.expanduser(
                    "~/TCDTIMIT/combinedSR/") + datasetName + "/binaryLipspeakers" + os.sep \
                               + 'allLipspeakersTest' + "_" + noiseType + "_" + "ratio" + str(ratio_dB) + '.pkl'
                allMfccs_test, allAudioLabels_test, allValidLabels_test, allValidAudioFrames_test = unpickle(testPath)


            test_cost, test_acc, test_topk_acc, nb_test_batches = self.val_epoch(runType=runType,
                                                                images=allImages_test,
                                                                mfccs=allMfccs_test,
                                                                validLabels=allValidLabels_test,
                                                                valid_frames=allValidAudioFrames_test,
                                                                batch_size=1)
            test_cost /= nb_test_batches
            test_acc = test_acc / nb_test_batches * 100
            test_topk_acc = test_topk_acc / nb_test_batches * 100
        else:
            test_cost, test_acc, test_topk_acc = self.evalTEST(testSpeakerFiles,
                                                              runType=runType,
                                                              sourceDataDir=database_binaryDir,
                                                              storeProcessed=storeProcessed,
                                                              processedDir=processedDir,
                                                              withNoise=withNoise, noiseType=noiseType, ratio_dB=ratio_dB)
        # # TODO: end remove


        logger.info("TEST results: ")
        logger.info("\t  test cost:        %s", test_cost)
        logger.info("\t  test acc rate:  %s %%", test_acc)
        logger.info("\t  test top 3 acc:  %s %%", test_topk_acc)
        if justTest: return


        logger.info("starting training for %s epochs...", num_epochs)
        # now run through the epochs
        for epoch in range(num_epochs):
            logger.info("\n\n\n Epoch %s started", epoch + 1)
            start_time = time.time()

            if self.loadPerSpeaker:
                train_cost, val_cost, val_acc, val_topk_acc = self.evalTRAINING(trainingSpeakerFiles, LR=LR,
                                                                            runType=runType,
                                                                            shuffleEnabled=shuffleEnabled,
                                                                            sourceDataDir=database_binaryDir,
                                                                            storeProcessed=storeProcessed,
                                                                            processedDir=processedDir,
                                                                            withNoise=withNoise,
                                                                                noiseType=noiseType, ratio_dB=ratio_dB)
            else:
                train_cost, nb_train_batches = self.train_epoch(runType=runType,
                                                                 images=allImages_train,
                                                                 mfccs=allMfccs_train,
                                                                 validLabels=allValidLabels_train,
                                                                 valid_frames=allValidAudioFrames_train,
                                                                 LR=LR)
                train_cost /= nb_train_batches

                val_cost, val_acc, val_topk_acc, nb_val_batches = self.val_epoch(runType=runType,
                                                                                 images=allImages_val,
                                                                                 mfccs=allMfccs_val,
                                                                                 validLabels=allValidLabels_val,
                                                                                 valid_frames=allValidAudioFrames_val,
                                                                                 batch_size=1)
                val_cost /= nb_val_batches
                val_acc = val_acc / nb_val_batches * 100
                val_topk_acc = val_topk_acc / nb_val_batches * 100
                

            # test if validation acc went up
            printTest = False
            resetNetwork=False
            if val_acc > best_val_acc:
                printTest = True
                if val_acc - best_val_acc > 0.2: self.epochsNotImproved = 0 #don't keep training if just a little bit of improvment
                best_val_acc = val_acc
                best_epoch = epoch + 1

                logger.info("\n\nBest ever validation score; evaluating TEST set...")

                if self.loadPerSpeaker:
                    test_cost, test_acc, test_topk_acc = self.evalTEST(testSpeakerFiles, runType=runType,
                                                                   sourceDataDir=database_binaryDir,
                                                                   storeProcessed=storeProcessed,
                                                                   processedDir=processedDir,
                                                                       withNoise=withNoise, noiseType=noiseType,
                                                                       ratio_dB=ratio_dB)
                else:
                    test_cost, test_acc, test_topk_acc, nb_test_batches = self.val_epoch(runType=runType,
                                                                                         images=allImages_test,
                                                                                         mfccs=allMfccs_test,
                                                                                         validLabels=allValidLabels_test,
                                                                                         valid_frames=allValidAudioFrames_test,
                                                                                         batch_size=1)
                    test_cost /= nb_test_batches
                    test_acc = test_acc / nb_test_batches * 100
                    test_topk_acc = test_topk_acc / nb_test_batches * 100

                logger.info("TEST results: ")
                logger.info("\t  test cost:        %s", test_cost)
                logger.info("\t  test acc rate:  %s %%", test_acc)
                logger.info("\t  test top 3 acc:  %s %%", test_topk_acc)

                self.best_cost = val_cost
                self.best_epoch = self.curr_epoch

                # get the parameters of the model we're training
                if runType == 'audio':         lout = self.audioNet_lout
                elif runType == 'lipreading':  lout = self.lipreading_lout
                elif runType == 'combined':    lout = self.combined_lout
                else: raise IOError("can't save network params; network output not found")

                self.best_param = L.get_all_param_values(lout)
                logger.info("New best model found!")
                if save_name is not None:
                    logger.info("Model saved as " + save_name)
                    self.save_model(save_name)

                # save top scores
                self.network_train_info['final_test_cost'] = test_cost
                self.network_train_info['final_test_acc'] = test_acc
                self.network_train_info['final_test_top3_acc'] = test_topk_acc

            else:   #reset to best model we had
                resetNetwork= True

            epoch_duration = time.time() - start_time

            # Then we logger.info the results for this epoch:
            logger.info("Epoch %s of %s took %s seconds", epoch + 1, num_epochs, epoch_duration)
            logger.info("  LR:                            %s", LR)
            logger.info("  training cost:                 %s", train_cost)
            logger.info("  validation cost:               %s", val_cost)
            logger.info("  validation acc rate:         %s %%", val_acc)
            logger.info("  validation top 3 acc rate:         %s %%", val_topk_acc)
            logger.info("  best epoch:                    %s", best_epoch)
            logger.info("  best validation acc rate:    %s %%", best_val_acc)
            if printTest:
                logger.info("  test cost:                 %s", test_cost)
                logger.info("  test acc rate:           %s %%", test_acc)
                logger.info("  test top 3 acc rate:    %s %%", test_topk_acc)

            # save the training info
            self.network_train_info['train_cost'].append(train_cost)
            self.network_train_info['val_cost'].append(val_cost)
            self.network_train_info['val_acc'].append(val_acc)
            self.network_train_info['val_topk_acc'].append(val_topk_acc)
            self.network_train_info['test_cost'].append(test_cost)
            self.network_train_info['test_acc'].append(test_acc)
            self.network_train_info['test_topk_acc'].append(test_topk_acc)

            nb_params = self.getParamsInfo()
            self.network_train_info['nb_params'] = nb_params

            store_path = save_name + '_trainInfo.pkl'
            saveToPkl(store_path, self.network_train_info)
            logger.info("Train info written to:\t %s", store_path)

            # decay the LR
            # LR *= LR_decay
            LR = self.updateLR(LR, LR_decay)

            if resetNetwork: self.setNetworkParams(runType)

            if self.epochsNotImproved > 3:
                logger.warning("\n\n NO MORE IMPROVEMENTS -> stop training")

                finalTestResults = self.finalNetworkEvaluation(save_name=save_name,
                                            database_binaryDir=database_binaryDir,
                                            processedDir=processedDir,
                                            runType=runType,
                                            storeProcessed=storeProcessed,
                                            testSpeakerFiles=testSpeakerFiles,
                                            withNoise=withNoise, noiseType=noiseType, ratio_dB=ratio_dB)
                break

        logger.info("Done.")
        return finalTestResults

    def loadPreviousResults(self, save_name, logger=logger_combinedtools):
        # try to load performance metrics of stored model
        best_val_acc = 0
        test_topk_acc = 0
        test_cost = 0
        test_acc = 0
        try:
            if os.path.exists(save_name + ".npz") and os.path.exists(save_name + "_trainInfo.pkl"):
                old_train_info = unpickle(save_name + '_trainInfo.pkl')
                if type(old_train_info) == dict:  # normal case
                    best_val_acc = max(old_train_info['val_acc'])
                    test_cost = min(old_train_info['test_cost'])
                    test_acc = max(old_train_info['test_acc'])
                    test_topk_acc = max(old_train_info['test_topk_acc'])
                    self.network_train_info = old_train_info  #load old train info so it won't get lost on retrain


                    if not 'final_test_cost' in self.network_train_info.keys():
                        self.network_train_info['final_test_cost'] = min(self.network_train_info['test_cost'])
                    if not 'final_test_acc' in self.network_train_info.keys():
                        self.network_train_info['final_test_acc'] = max(self.network_train_info['test_acc'])
                    if not 'final_test_top3_acc' in self.network_train_info.keys():
                        self.network_train_info['final_test_top3_acc'] = max(self.network_train_info['test_topk_acc'])
                else:
                    logger.warning("old trainInfo found, but wrong format: %s", save_name + "_trainInfo.pkl")
                    # do nothing
            else:
                return -1,-1
        except:
            logger.warning("No old trainInfo found...")
            pass
        return best_val_acc, test_acc

    # evaluate network on test set.
    # Combined network  -> evaluate audio, lipreading and then combined network
    # Audio network     -> evaluate audio
    # Lipreading        -> evaluate lipreading
    def finalNetworkEvaluation(self, save_name, database_binaryDir, processedDir, runType, testSpeakerFiles,
                               withNoise=False, noiseType='white', ratio_dB=-3,  datasetName='TCDTIMIT', roundParams=False,
                               storeProcessed=False,  nbPhonemes=39, viseme=False, withPreds=False, logger=logger_combinedtools):
        if "volunteers" in self.test_dataset :
            loadPerSpeaker = True
        else: loadPerSpeaker = self.loadPerSpeaker #load default value
        # else, load data that is given (True -> volunteers, False -> lipspeakers)
        if viseme: nbPhonemes = 12

        # print what kind of network we're running
        if runType == 'lipreading': networkType = "lipreading " + self.lipreadingType
        else: networkType = runType
        logger.info(" \n\n Running FINAL evaluation on Test set... (%s network type)", networkType)

        # get the data to test
        store_path = save_name + '_trainInfo.pkl'  #dictionary with lists that contain training info for each epoch (train/val/test accuracy, cost etc)
        self.network_train_info = unpickle(store_path)
        # for the lipspeaker files that are all loaded in memory at once, we still need to get the data
        if not loadPerSpeaker:  # load all the lipspeakers in memory, then don't touch the files -> no reloading needed = faster
            testPath = os.path.expanduser("~/TCDTIMIT/combinedSR/TCDTIMIT/binaryLipspeakers/allLipspeakersTest.pkl")
            if viseme:
                testPath = testPath.replace(".pkl", "_viseme.pkl")
            allImages_test, allMfccs_test, allAudioLabels_test, allValidLabels_test, allValidAudioFrames_test = unpickle(
                testPath)
            if withNoise:
                testPath= os.path.expanduser("~/TCDTIMIT/combinedSR/") + datasetName + "/binaryLipspeakers" + os.sep \
                              + 'allLipspeakersTest' + "_" + noiseType + "_" + "ratio" + str(ratio_dB) + '.pkl'
                allMfccs_test, allAudioLabels_test, allValidLabels_test, allValidAudioFrames_test = unpickle(testPath)

        if loadPerSpeaker:
            test_cost, test_acc, test_topk_acc = self.evalTEST(testSpeakerFiles, runType=runType,
                                                               sourceDataDir=database_binaryDir,
                                                               storeProcessed=storeProcessed,
                                                               processedDir=processedDir,
                                                               withNoise=withNoise, noiseType=noiseType,
                                                               ratio_dB=ratio_dB)
        else:
            if withPreds:
                test_cost, test_acc, test_topk_acc, nb_test_batches, predictions = self.val_epoch_withPreds(runType=runType,
                                                                                     images=allImages_test,
                                                                                     mfccs=allMfccs_test,
                                                                                     validLabels=allValidLabels_test,
                                                                                     valid_frames=allValidAudioFrames_test,
                                                                                     batch_size=1)
                confMatrix = self.getConfusionMatrix(allValidLabels_test, predictions, nbPhonemes)
                saveToPkl(save_name + "_confusionMatrix.pkl", confMatrix)
            else:
                test_cost, test_acc, test_topk_acc, nb_test_batches = self.val_epoch(runType=runType,
                                                                                    images=allImages_test,
                                                                                    mfccs=allMfccs_test,
                                                                                    validLabels=allValidLabels_test,
                                                                                    valid_frames=allValidAudioFrames_test,
                                                                                    batch_size=1)
            test_cost /= nb_test_batches
            test_acc = test_acc / nb_test_batches * 100
            test_topk_acc = test_topk_acc / nb_test_batches * 100

        logger.info("FINAL TEST results on %s: ", runType)
        if roundParams: logger.info("ROUND_PARAMS")
        logger.info("\t  %s test cost:        %s", runType, test_cost)
        logger.info("\t  %s test acc rate:  %s %%", runType, test_acc)
        logger.info("\t  %s test top 3 acc:  %s %%", runType, test_topk_acc)

        if self.test_dataset != self.dataset:
            testType = "_" + self.test_dataset
        else:
            testType = ""
        if roundParams:
            testType = "_roundParams" + testType


        if runType != 'lipreading' and withNoise:
            print(noiseType + "_" + "ratio" + str(ratio_dB) + testType)
            self.network_train_info[
                'final_test_cost_' + noiseType + "_" + "ratio" + str(ratio_dB) + testType] = test_cost
            self.network_train_info['final_test_acc_' + noiseType + "_" + "ratio" + str(ratio_dB) + testType] = test_acc
            self.network_train_info[
                'final_test_top3_acc_' + noiseType + "_" + "ratio" + str(ratio_dB) + testType] = test_topk_acc
        else:
            self.network_train_info['final_test_cost' + testType] = test_cost
            self.network_train_info['final_test_acc' + testType] = test_acc
            self.network_train_info['final_test_top3_acc' + testType] = test_topk_acc

        nb_params = self.getParamsInfo()
        self.network_train_info['nb_params'] = nb_params

        saveToPkl(store_path, self.network_train_info)

        return test_cost, test_acc, test_topk_acc

    def getConfusionMatrix(self,y_test, maxprob, nbClasses):
        import theano
        from theano import tensor as T
        x = T.ivector('x')
        classes = T.scalar('n_classes')
        onehot = T.eq(x.dimshuffle(0, 'x'), T.arange(classes).dimshuffle('x', 0))
        oneHot = theano.function([x, classes], onehot)
        examples = T.scalar('n_examples')
        y = T.imatrix('y')
        y_pred = T.imatrix('y_pred')
        confMat = T.dot(y.T, y_pred) / examples
        confusionMatrix = theano.function(inputs=[y, y_pred, examples], outputs=confMat)

        def confusion_matrix(targets, preds, n_class):
            try:assert len(targets) >= len(preds)
            except: import pdb;pdb.set_trace()
            targets = targets[:len(preds)]
            targetsFlat = []; predsFlat = []
            for i in range(len(targets)):
                targetsFlat += list(targets[i])
                predsFlat += list(preds[i])
            return confusionMatrix(oneHot(targetsFlat, n_class), oneHot(predsFlat, n_class), len(targetsFlat))

        return confusion_matrix(y_test, maxprob, nbClasses)

    def updateLR(self, LR, LR_decay, logger=logger_combinedtools):
        this_acc = self.network_train_info['val_acc'][-1]
        this_cost = self.network_train_info['val_cost'][-1]
        try:
            last_acc = self.network_train_info['val_acc'][-2]
            last_cost = self.network_train_info['val_cost'][-2]
        except:
            last_acc = -10
            last_cost = 10 * this_cost  # first time it will fail because there is only 1 result stored

        # only reduce LR if not much improvment anymore
        if this_cost / float(last_cost) >= 0.98 or this_acc-last_acc < 0.2:
            logger.info(" Error not much reduced: %s vs %s. Reducing LR: %s", this_cost, last_cost, LR * LR_decay)
            self.epochsNotImproved += 1
            return LR * LR_decay
        else:
            self.epochsNotImproved = max(self.epochsNotImproved - 1, 0)  # reduce by 1, minimum 0
            return LR
