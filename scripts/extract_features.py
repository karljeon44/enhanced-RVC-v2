"""modified from https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/main/extract_feature_print.py
by karljeon44
"""
import argparse
import os
import traceback

import numpy as np
import soundfile as sf
import torch
import torch.nn.functional as F
from fairseq import checkpoint_utils
from transformers import HubertModel

from utils.misc_utils import get_device, HUBERT_FPATH

os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
os.environ["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] = "0.0"

argparser = argparse.ArgumentParser()
argparser.add_argument('exp_dir', help='experiment dirpath')
argparser.add_argument('-k', '--kor', action='store_true', help='whether to use Hubert Korean')

args = argparser.parse_args()
exp_dir = args.exp_dir
device = get_device()

print(exp_dir)
wavPath = "%s/1_16k_wavs" % exp_dir
outPath = "%s/3_feature768" % exp_dir
os.makedirs(outPath, exist_ok=True)


# wave must be 16k, hop_size=320
def readwave(wav_path, normalize=False):
  wav, sr = sf.read(wav_path)
  assert sr == 16000
  feats = torch.from_numpy(wav).float()
  if feats.dim() == 2:  # double channels
    feats = feats.mean(-1)
  assert feats.dim() == 1, feats.dim()
  if normalize:
    with torch.no_grad():
      feats = F.layer_norm(feats, feats.shape)
  feats = feats.view(1, -1)
  return feats


# HuBERT model
if args.kor:
  model = HubertModel.from_pretrained("team-lucid/hubert-base-korean")
  model = model.to(device)
  print("move model to %s" % device)
  if device not in ["mps", "cpu"]:
    model = model.half()
  model.eval()

else:
  print("load model(s) from {}".format(HUBERT_FPATH))
  # if hubert model is exist
  if os.access(HUBERT_FPATH, os.F_OK) == False:
    print("Error: Extracting is shut down because hubert does not exist, you may download it from https://huggingface.co/lj1995/VoiceConversionWebUI/tree/main")
    exit(0)
  models, saved_cfg, task = checkpoint_utils.load_model_ensemble_and_task([HUBERT_FPATH], suffix="",)
  model = models[0]
  model = model.to(device)
  print("move model to %s" % device)
  if device not in ["mps", "cpu"]:
    model = model.half()
  model.eval()

todo = sorted(list(os.listdir(wavPath)))
n = max(1, len(todo) // 10)  # 最多打印十条
if len(todo) == 0:
  print("no-feature-todo")
else:
  print("all-feature-%s" % len(todo))
  for idx, file in enumerate(todo):
    try:
      if file.endswith(".wav"):
        wav_path = "%s/%s" % (wavPath, file)
        out_path = "%s/%s" % (outPath, file.replace("wav", "npy"))

        if os.path.exists(out_path):
          continue

        feats = readwave(wav_path, normalize=saved_cfg.task.normalize)
        feats = feats.half().to(device) if device not in ["mps", "cpu"] else feats.to(device)

        if args.kor:
          with torch.no_grad():
            feats = model(feats)['last_hidden_state']

        else:
          padding_mask = torch.BoolTensor(feats.shape).fill_(False)
          inputs = {
            "source": feats,
            "padding_mask": padding_mask.to(device),
            "output_layer": 12,  # layer 9
          }
          with torch.no_grad():
            logits = model.extract_features(**inputs)
            feats = logits[0]

        feats = feats.squeeze(0).float().cpu().numpy()
        if np.isnan(feats).sum() == 0:
          np.save(out_path, feats, allow_pickle=False)
        else:
          print("%s-contains nan" % file)
        if idx % n == 0:
          print("now-%s,all-%s,%s,%s" % (len(todo), idx, file, feats.shape))
    except:
      print(traceback.format_exc())
  print("all-feature-done")
