
import numpy as np
from skimage.util import view_as_windows

from .utils import mask_CUDA_VISIBLE_DEVICES
from .preprocessMic import preprocessMic, padToRegularSize, getDownFactor, resizeMic
from math import ceil

from .config import MODEL_IMG_SIZE

BATCH_SIZE = 16


class MaskPredictor(object):

  '''
  Class used to compute 0. to 1. masks given one numpy array of shape HxW that represents a micrograph
  '''
  def __init__(self, deepLearningModelFname, boxSize , gpus=[0], strideFactor=2):
    '''
    :param deepLearningModelFname (str): a path where the deep learning model will be loaded
    :param boxSize (int): estimated particle boxSize in pixels
    :param gpus (list of gpu ids (ints) or None): If None, CPU only mode will be employed.
    :param strideFactor (int): Overlapping between windows. Micrographs are divided into patches and each processed individually.
                         The overlapping factor indicates how many times a given row/column is processed by the network. The
                         bigger the better the predictions, but higher computational cost.
    '''
    mask_CUDA_VISIBLE_DEVICES(gpus)
    import keras
    self.model = keras.models.load_model(deepLearningModelFname, {})
    self.boxSize = boxSize
    self.strideFactor= strideFactor
    if gpus is not None:
      if len(gpus) > 1:
        self.model = keras.utils.multi_gpu_model(self.model, gpus=gpus)

  def getDownFactor(self):
    '''
    MaskPredictor preprocess micrographs before Nnet computation. First step is donwsampling using a donwsampling factor
    that depends on particle boxSize. This function computes the donwsampling factor

    :return (float): the donwsampling factor that MaskPredictor uses internally when preprocessing the micrographs
    '''
    return getDownFactor(self.boxSize)

  def predictFake(self, arrayOfPatches):
    '''
    debuging purposes only
    :param arrayOfPatches: an array of patches obtained from a micrograph
    :return: an array of thresholized patches
    '''
    for i in range(arrayOfPatches.shape[0]):
      arrayOfPatches[i][arrayOfPatches[i] > 0.5] = 1
      arrayOfPatches[i][arrayOfPatches[i] <= 0.5] = 0
    return arrayOfPatches

  def predictMask(self, inputMic):
    '''
    Obtains a contamination mask for a given inputMic

    :param inputMic (np.array shape HxW): the micrograph to clean
    :return: mask (np.array shape HxW): a mask that ranges from 0. to 1. ->
                   0. meaning clean area and 1. contaminated area.
    '''

    originalShape = inputMic.shape
    mic = preprocessMic(inputMic, self.boxSize)

    # print("Donwsampled from %s --> %s"%( originalShape, mic.shape ) )


    mask_list = []
    fixedJump_list = []

    padding_list = []
    windows_list = []
    N_ROTS = 4
    for rot in range(N_ROTS):
      img = np.rot90(mic, rot)
      paddedMic, paddingTuples = padToRegularSize(img, MODEL_IMG_SIZE, self.strideFactor, fillWith0=True)
      windows, originalWinShape = self.extractPatches(paddedMic)
      windows_list.append(windows)
      padding_list.append((paddedMic.shape, paddingTuples))

    windows_pred = self.model.predict(np.concatenate(windows_list, axis=0), batch_size=BATCH_SIZE, verbose=1)
    step = windows_pred.shape[0] // N_ROTS
    for i in range(N_ROTS):
      micro_pred = windows_pred[i * step:(i + 1) * step, ...]
      micro_pred = micro_pred.reshape(originalWinShape)
      micShape, paddingTuples = padding_list[i]
      mask, jumpFound = self.return_as_oneMic(micro_pred, micShape, paddingTuples, MODEL_IMG_SIZE)
      mask_list.append(np.rot90(mask, 4 - i))
      fixedJump_list.append(jumpFound)

    if True in fixedJump_list:
      mask_list = [mask for mask, fixedJump in zip(mask_list, fixedJump_list) if fixedJump == True]
    mask = sum(mask_list) / len(mask_list)
    mask = resizeMic(mask, originalShape)

    # from matplotlib import pyplot as plt; fig= plt.figure(); fig.add_subplot(311); plt.imshow(inputMic, cmap="gray"); fig.add_subplot(312); plt.imshow(mic, cmap="gray"); fig.add_subplot(313); plt.imshow(mask); plt.show()
    return mask

  def extractPatches(self, paddedMic):
    windows = view_as_windows(paddedMic, (MODEL_IMG_SIZE, MODEL_IMG_SIZE), step=MODEL_IMG_SIZE // self.strideFactor)
    windowsOriShape = windows.shape
    windows = windows.reshape((-1, MODEL_IMG_SIZE, MODEL_IMG_SIZE, 1))
    return windows, windowsOriShape

  def return_as_oneMic(self, predWindows, micShape, paddingTuples, patchSize):
    stride = patchSize // self.strideFactor
    endPoint_height, endPoint_width = micShape[:2]
    endPoint_height -= patchSize - 1
    endPoint_width -= patchSize - 1
    micro_out = np.zeros(micShape)
    micro_weights = np.zeros(micShape)
    for i, i_inMat in enumerate(range(0, endPoint_height, stride)):
      for j, j_inMat in enumerate(range(0, endPoint_width, stride)):
        micro_out[i_inMat: i_inMat + patchSize, j_inMat: j_inMat + patchSize] += predWindows[i, j, ...]
        micro_weights[i_inMat: i_inMat + patchSize, j_inMat: j_inMat + patchSize] += 1.

    micro_out = micro_out / micro_weights
    micro_out = micro_out[paddingTuples[0][0]:-paddingTuples[0][1], paddingTuples[1][0]:-paddingTuples[1][1], ...]

    jumpFound = False
    micro_out, found = fixJumpInBorders(micro_out, axis=0, stride=stride)
    jumpFound = jumpFound or found
    micro_out, found = fixJumpInBorders(micro_out, axis=1, stride=stride)
    jumpFound = jumpFound or found
    return micro_out, jumpFound

  def close(self):
    del self.model
    import keras
    keras.backend.clear_session()

  def __exit__(self, exc_type, exc_value, tb):
    self.close()

  def __enter__(self):
    return self

def putNewVal(x, initPoint, value, axis, toTheRight=True):
  if axis == 0:
    if toTheRight:
      x[initPoint:x.shape[0], :] = value
    else:
      x[0:initPoint, :] = value
  else:
    if toTheRight:
      x[:, initPoint:x.shape[1]] = value
    else:
      x[:, 0:initPoint] = value
  return x


def fixJumpInBorders(micro_out, axis, stride):
  candidates = []
  for i in range(1, int(ceil(micro_out.shape[axis] / float(stride)))):
    currentRow = np.take(micro_out, [stride * i - 1], axis=axis)
    nextRow = np.take(micro_out, [stride * i], axis=axis)
    mean_dif_frac = np.mean(nextRow - currentRow) / np.mean(currentRow)
    if mean_dif_frac < -0.4:
      candidateBlock = np.take(micro_out, range(stride * i, min(micro_out.shape[axis], stride * (i + 1) - 1)),
                               axis=axis)
      candidates += [(i * stride, np.mean(candidateBlock))]

  found = False
  if len(candidates) > 1:
    idxs, mean_vals = zip(*candidates)
    if np.all(np.diff(mean_vals) < 0):
      print("Padding effect found in axis %d. Correcting" % (axis))
      micro_out = putNewVal(micro_out, idxs[0], np.take(micro_out, [idxs[0] - 1], axis=axis), axis)
      found = True
  return micro_out, found
