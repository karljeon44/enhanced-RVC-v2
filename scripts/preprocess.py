"""modified from https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/main/trainset_preprocess_pipeline_print.py
by karljeon44
"""
import argparse
import logging
import multiprocessing

import librosa
import numpy as np
import os
import traceback
from scipy import signal
from scipy.io import wavfile

from utils.misc_utils import load_audio
from utils.slicer2 import Slicer

numba_logger = logging.getLogger('numba')
numba_logger.setLevel(logging.WARNING)


argparser = argparse.ArgumentParser()
argparser.add_argument('input_dir', help='input dataset dirpath')
argparser.add_argument('exp_dir', help='output experiment dirpath')
argparser.add_argument('-n', '--num_proc', type=int, default=4, help='number of processes to use')
argparser.add_argument('-sr', '--sample_rate', type=str.lower, default='48k', help='target sample rate')

args= argparser.parse_args()
inp_root = args.input_dir
exp_dir = args.exp_dir
n_p = args.num_proc
if args.sample_rate == '48k':
  sr = 48000
elif args.sample_rate == '40k':
  sr = 40000
elif args.sample_rate == '32k':
  sr = 32000
else:
  raise ValueError(f'Given sample rate (`{args.sample_rate}`) not understood')

print(f"Using Sample Rate: {int(sr//1000)}K")

os.makedirs(exp_dir, exist_ok=True)
mutex = multiprocessing.Lock()

def println(strr):
  mutex.acquire()
  print(strr)
  mutex.release()


class PreProcess:
  def __init__(self, sr, exp_dir):
    self.slicer = Slicer(
      sr=sr,
      threshold=-42,
      min_length=1500,
      min_interval=400,
      hop_size=15,
      max_sil_kept=500,
    )
    self.sr = sr
    self.bh, self.ah = signal.butter(N=5, Wn=48, btype="high", fs=self.sr)
    self.per = 3.0
    self.overlap = 0.3
    self.tail = self.per + self.overlap
    self.max = 0.9
    self.alpha = 0.75
    self.exp_dir = exp_dir
    self.gt_wavs_dir = "%s/0_gt_wavs" % exp_dir
    self.wavs16k_dir = "%s/1_16k_wavs" % exp_dir
    os.makedirs(self.exp_dir, exist_ok=True)
    os.makedirs(self.gt_wavs_dir, exist_ok=True)
    os.makedirs(self.wavs16k_dir, exist_ok=True)

    self.speaker_mapping = {}

  # def norm_write(self, tmp_audio, idx0, idx1):
  def norm_write(self, tmp_audio, idx0, idx1, spk_id=None):
    tmp_max = np.abs(tmp_audio).max()
    if tmp_max > 2.5:
      print("%s-%s-%s-filtered" % (idx0, idx1, tmp_max))
      return
    tmp_audio = (tmp_audio / tmp_max * (self.max * self.alpha)) + (1 - self.alpha) * tmp_audio

    if spk_id is not None:
      msg1 = "%s/%s_%s_%s.wav" % (self.gt_wavs_dir, idx0, idx1, spk_id)
      msg2 = "%s/%s_%s_%s.wav" % (self.wavs16k_dir, idx0, idx1, spk_id)
    else:
      msg1 = "%s/%s_%s.wav" % (self.gt_wavs_dir, idx0, idx1)
      msg2 = "%s/%s_%s.wav" % (self.wavs16k_dir, idx0, idx1)

    wavfile.write(msg1, self.sr, tmp_audio.astype(np.float32),)
    tmp_audio = librosa.resample(tmp_audio, orig_sr=self.sr, target_sr=16000 )  # , res_type="soxr_vhq"
    wavfile.write(msg2, 16000, tmp_audio.astype(np.float32),)

  def pipeline(self, path, idx0):
    try:
      audio = load_audio(path, self.sr)
      # zero phased digital filter cause pre-ringing noise...
      # audio = signal.filtfilt(self.bh, self.ah, audio)
      audio = signal.lfilter(self.bh, self.ah, audio)

      spk_id = None
      try:
        fname_split = os.path.basename(os.path.splitext(path)[0]).split('_')
        spk_id = str(int(fname_split[1]))

        if spk_id not in self.speaker_mapping:
          self.speaker_mapping[spk_id] = str(len(self.speaker_mapping))
          print(f"Spk ID mapped from SINGER_{spk_id} -> {self.speaker_mapping[spk_id]}")

        spk_id = self.speaker_mapping[spk_id]

      except (ValueError, IndexError):
        pass
      # print("Spk ID:", repr(spk_id))

      idx1 = 0
      for audio in self.slicer.slice(audio):
        i = 0
        while 1:
          start = int(self.sr * (self.per - self.overlap) * i)
          i += 1
          if len(audio[start:]) > self.tail * self.sr:
            tmp_audio = audio[start : start + int(self.per * self.sr)]
            self.norm_write(tmp_audio, idx0, idx1, spk_id)
            idx1 += 1
          else:
            tmp_audio = audio[start:]
            idx1 += 1
            break
        self.norm_write(tmp_audio, idx0, idx1, spk_id)
      println("`%s` -> Suc." % path)
    except:
      println("`%s` -> %s" % (path, traceback.format_exc()))

  def pipeline_mp(self, infos):
    for path, idx0 in infos:
      self.pipeline(path, idx0)

  def pipeline_mp_inp_dir(self, inp_root, n_p):
    try:
      valid_fnames = [x for x in list(os.listdir(inp_root)) if x.endswith('.wav')]
      infos = [("%s/%s" % (inp_root, name), idx) for idx, name in enumerate(sorted(valid_fnames))]
      if n_p == 1:
        for i in range(n_p):
          self.pipeline_mp(infos[i::n_p])
      else:
        ps = []
        for i in range(n_p):
          p = multiprocessing.Process(target=self.pipeline_mp, args=(infos[i::n_p],))
          ps.append(p)
          p.start()
        for i in range(n_p):
          ps[i].join()
    except:
      println("Fail. %s" % traceback.format_exc())


def preprocess_trainset(inp_root, sr, n_p, exp_dir):
  pp = PreProcess(sr, exp_dir)
  println("start preprocess")
  pp.pipeline_mp_inp_dir(inp_root, n_p)
  println("end preprocess")


if __name__ == "__main__":
  preprocess_trainset(inp_root, sr, n_p, exp_dir)
