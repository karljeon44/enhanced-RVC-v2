"""modified from https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/main/train_nsf_sim_cache_sid_load_pretrain.py
by karljeon44
"""
import datetime
import os
from random import shuffle
from time import sleep, time as ttime

import torch
torch.backends.cudnn.deterministic = False
torch.backends.cudnn.benchmark = False
import torch.nn.functional as F
from torch.cuda.amp import autocast, GradScaler
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from model import commons
from model.discriminator import Discriminator, MultiPeriodDiscriminatorV2, MultiScaleSTFTDiscriminator
from model.losses import generator_loss, discriminator_loss, feature_loss, kl_loss, MultiResolutionSTFTLoss
from model.mel_processing import mel_spectrogram_torch, spec_to_mel_torch
from model.models import SynthesizerTrnMs768NSFsid
from utils import misc_utils
from utils.data_utils import (
  TextAudioLoaderMultiNSFsid,
  TextAudioLoader,
  TextAudioCollateMultiNSFsid,
  TextAudioCollate,
  DistributedBucketSampler,
)
from utils.process_ckpt import savee

GLOBAL_STEP = 0


class EpochRecorder:
  def __init__(self):
    self.last_time = ttime()

  def record(self):
    now_time = ttime()
    elapsed_time = now_time - self.last_time
    self.last_time = now_time
    elapsed_time_str = str(datetime.timedelta(seconds=elapsed_time))
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"[{current_time}] | ({elapsed_time_str})"


def main():
  global GLOBAL_STEP

  hps = misc_utils.get_hparams()
  assert hps.version == 'v2', "ervc-v2 only compatible with V2"

  logger = misc_utils.get_logger(hps.model_dir)
  logger.info(hps)
  writer = SummaryWriter(log_dir=hps.model_dir)
  writer_eval = SummaryWriter(log_dir=os.path.join(hps.model_dir, "eval"))

  torch.manual_seed(hps.train.seed)
  device = misc_utils.get_device()

  if hps.if_f0:
    train_dataset = TextAudioLoaderMultiNSFsid(hps.data.training_files, hps.data)
  else:
    train_dataset = TextAudioLoader(hps.data.training_files, hps.data)
  train_sampler = DistributedBucketSampler(
    train_dataset,
    hps.train.batch_size,
    # [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1200,1400],  # 16s
    [100, 200, 300, 400, 500, 600, 700, 800, 900],  # 16s
    num_replicas=1,
    rank=0,
    shuffle=True,
  )

  # It is possible that dataloader's workers are out of shared memory. Please try to raise your shared memory limit.
  # num_workers=8 -> num_workers=4
  if hps.if_f0:
    collate_fn = TextAudioCollateMultiNSFsid()
  else:
    collate_fn = TextAudioCollate()
  train_loader = DataLoader(
    train_dataset,
    num_workers=4,
    shuffle=False,
    pin_memory=True,
    collate_fn=collate_fn,
    batch_sampler=train_sampler,
    persistent_workers=True,
    prefetch_factor=8,
  )

  if hps.if_f0:
    net_g = SynthesizerTrnMs768NSFsid(
      hps.data.filter_length // 2 + 1,
      hps.train.segment_size // hps.data.hop_length,
      **hps.model, # now includes `snake`
      is_half=hps.train.fp16_run,
      sr=hps.sample_rate,
    )
  else:
    breakpoint() # shouldn't reach here yet
    # net_g = SynthesizerTrnMs768NSFsid_nono(
    #   hps.data.filter_length // 2 + 1,
    #   hps.train.segment_size // hps.data.hop_length,
    #   **hps.model,
    #   is_half=hps.train.fp16_run,
    # )

  # discriminator init
  if hps.model.mrd:
    net_d = Discriminator(hps.resolutions, use_spectral_norm=hps.model.use_spectral_norm)
  elif hps.model.msstftd:
    net_d = MultiScaleSTFTDiscriminator(hps.model.msstftd_filters)
  else:
    net_d = MultiPeriodDiscriminatorV2(use_spectral_norm=hps.model.use_spectral_norm)

  net_g = net_g.to(device)
  net_d = net_d.to(device)

  # init optim
  optim_g = torch.optim.AdamW(net_g.parameters(), hps.train.learning_rate, betas=hps.train.betas, eps=hps.train.eps,)
  optim_d = torch.optim.AdamW(net_d.parameters(), hps.train.learning_rate, betas=hps.train.betas, eps=hps.train.eps,)

  try:  # 如果能加载自动resume

    _, _, _, epoch_str = misc_utils.load_checkpoint(misc_utils.latest_checkpoint_path(
      hps.model_dir, "G_*.pth"), net_g, optim_g, load_opt=hps.load_opt)
    _, _, _, epoch_str = misc_utils.load_checkpoint(misc_utils.latest_checkpoint_path(
      hps.model_dir, "D_*.pth"), net_d, optim_d, load_opt=hps.load_opt)

    logger.info("loaded from latest checkpoints (optimizer loaded: %s) " % hps.load_opt)
    GLOBAL_STEP = (epoch_str - 1) * len(train_loader)
  except:  # 如果首次不能加载，加载pretrain
    epoch_str = 1
    GLOBAL_STEP = 0

    if hps.pretrainG != "":
      logger.info("loading pretrained Generator from `%s`" % hps.pretrainG)
      pg = torch.load(hps.pretrainG, map_location="cpu")["model"]
      if hps.model.spk_embed_dim != 109:
        pg = {k: v for k, v in pg.items() if not k.startswith('emb_g')}

      # if hps.if_pretrain and hps.model.snake:
      #   logger.info("Pretraining Generator with Snake in 32k")
      #   # drop any keys with 'dec' prefix, will print `_IncompatibleKeys` with missing keys
      #   pg = {k:v for k,v in pg.items() if 'act' not in k}

      print(net_g.load_state_dict(pg, strict=False))

    if hps.pretrainD != "":
      if hps.if_pretrain and hps.model.mrd:
        logger.info("loading pretrained Multi-Period DiscriminatorV2 from `%s`" % hps.pretrainD)
        print(net_d.MPD.load_state_dict(torch.load(hps.pretrainD, map_location="cpu")["model"]))
      else:
        logger.info("loading pretrained Discriminator from `%s`" % hps.pretrainD)
        # # turning off `strict` flag to make room for SNAKE activations (instead of Leaky ReLU)
        # print(net_d.load_state_dict(torch.load(hps.pretrainD, map_location="cpu")["model"], strict=False))
        print(net_d.load_state_dict(torch.load(hps.pretrainD, map_location="cpu")["model"]))

    if hps.if_pretrain and hps.model.mrd and hps.pretrainS != "":
      logger.info("loading pretrained Multi-Resolution Discriminator from `%s`" % hps.pretrainS)
      sd_dict = torch.load(hps.pretrainS, map_location='cpu')['model_d']
      mrd_dict = {f'{k.replace("MRD.", "")}':v for k,v in sd_dict.items() if k.startswith('MRD')}
      print(net_d.MRD.load_state_dict(mrd_dict))

    if hps.if_pretrain and hps.model.snake and hps.sample_rate == '32k' and hps.pretrainV != "":
      logger.info("==> loading Snake pretrained weights for pre-training from BigVGAN 32k at `%s`" % hps.pretrainV)
      vg_dict = torch.load(hps.pretrainV, map_location='cpu')['model_g']
      vg_dict = {k: v for k, v in vg_dict.items() if not 'activation' in k}
      print("VG Dict Keys:", vg_dict.keys())
      print(net_g.dec.load_state_dict(vg_dict, strict=False))

  print(net_d)
  print(net_g)

  # breakpoint()
  logger.info("Model Summary")
  logger.info('Discriminator init type: %s', type(net_d))
  logger.info('Generator Vocoder init type: %s', type(net_g.dec))
  logger.info("D Number of Trainable Params: {:,}".format(sum(p.numel() for p in net_d.parameters())))
  if hps.model.mrd:
    logger.info("=> MRD Number of Trainable Params: {:,}".format(sum(p.numel() for p in net_d.MRD.parameters())))
  logger.info("G Number of Trainable Params: {:,}".format(sum(p.numel() for p in net_g.parameters())))

  scheduler_g = torch.optim.lr_scheduler.ExponentialLR(optim_g, gamma=hps.train.lr_decay, last_epoch=epoch_str-2)
  scheduler_d = torch.optim.lr_scheduler.ExponentialLR(optim_d, gamma=hps.train.lr_decay, last_epoch=epoch_str-2)
  scaler = GradScaler(enabled=hps.train.fp16_run)

  cache = []
  for epoch in range(epoch_str, hps.train.epochs+1):
    train_and_evaluate(
      epoch,
      hps=hps,
      nets=[net_g, net_d],
      optims=[optim_g, optim_d],
      scaler=scaler,
      loaders=[train_loader, None],
      logger=logger,
      writers=[writer, writer_eval],
      cache=cache,
      device=device
    )

    scheduler_g.step()
    scheduler_d.step()


def train_and_evaluate(epoch, hps, nets, optims, scaler, loaders, logger, writers, cache, device):
  global GLOBAL_STEP

  net_g, net_d = nets
  optim_g, optim_d = optims
  train_loader, eval_loader = loaders
  writer, writer_eval = writers

  train_loader.batch_sampler.set_epoch(epoch)

  net_g.train()
  net_d.train()

  # may not be used but just init for now
  stft_criterion = None
  if hps.model.mrstft:
    stft_criterion = MultiResolutionSTFTLoss(hps.resolutions, weight_by_factor=hps.model.weighted_mrstft, device=device)

  # Prepare data iterator
  if hps.if_cache_data_in_gpu:
    # Use Cache
    data_iterator = cache
    if not cache:
      # Make new cache
      for batch_idx, info in enumerate(train_loader):
        # Unpack
        pitch = pitchf = None
        if hps.if_f0 == 1:
          phone,phone_lengths,pitch,pitchf,spec,spec_lengths,wave,wave_lengths,sid = info
        else:
          phone,phone_lengths,spec,spec_lengths,wave,wave_lengths,sid = info

        # to device
        phone = phone.to(device)
        phone_lengths = phone_lengths.to(device)
        if hps.if_f0:
          pitch = pitch.to(device)
          pitchf = pitchf.to(device)
        sid = sid.to(device)
        spec = spec.to(device)
        spec_lengths = spec_lengths.to(device)
        wave = wave.to(device)
        wave_lengths = wave_lengths.to(device)

        # Cache on list
        if hps.if_f0:
          cache.append((batch_idx, (phone,phone_lengths,pitch,pitchf,spec,spec_lengths,wave,wave_lengths,sid)))
        else:
          cache.append((batch_idx, (phone,phone_lengths,spec,spec_lengths,wave,wave_lengths,sid)))
    else:
      # Load shuffled cache
      shuffle(cache)
  else:
    # Loader
    data_iterator = enumerate(train_loader)

  # Run steps
  epoch_recorder = EpochRecorder()
  for batch_idx, info in data_iterator:
    # Data
    ## Unpack
    pitch = pitchf = None
    if hps.if_f0:
      phone,phone_lengths,pitch,pitchf,spec,spec_lengths,wave,wave_lengths,sid = info
    else:
      phone, phone_lengths, spec, spec_lengths, wave, wave_lengths, sid = info

    ## Load on CUDA
    if not hps.if_cache_data_in_gpu:
      phone = phone.to(device)
      phone_lengths = phone_lengths.to(device)
      if hps.if_f0:
        pitch = pitch.to(device)
        pitchf = pitchf.to(device)
      sid = sid.to(device)
      spec = spec.to(device)
      spec_lengths = spec_lengths.to(device)
      wave = wave.to(device)

    # Calculate
    with autocast(enabled=hps.train.fp16_run):
      if hps.if_f0:
        y_hat,ids_slice,x_mask,z_mask,(z, z_p, m_p, logs_p, m_q, logs_q) = net_g(phone, phone_lengths, pitch, pitchf, spec, spec_lengths, sid)
      else:
        y_hat,ids_slice,x_mask,z_mask,(z, z_p, m_p, logs_p, m_q, logs_q) = net_g(phone, phone_lengths, spec, spec_lengths, sid)

      mel = spec_to_mel_torch(
        spec,
        hps.data.filter_length,
        hps.data.n_mel_channels,
        hps.data.sampling_rate,
        hps.data.mel_fmin,
        hps.data.mel_fmax,
      )
      y_mel = commons.slice_segments(mel, ids_slice, hps.train.segment_size // hps.data.hop_length)

      with autocast(enabled=False):
        y_hat_mel = mel_spectrogram_torch(
          y_hat.float().squeeze(1),
          hps.data.filter_length,
          hps.data.n_mel_channels,
          hps.data.sampling_rate,
          hps.data.hop_length,
          hps.data.win_length,
          hps.data.mel_fmin,
          hps.data.mel_fmax,
        )
      if hps.train.fp16_run:
        y_hat_mel = y_hat_mel.half()
      wave = commons.slice_segments(wave, ids_slice * hps.data.hop_length, hps.train.segment_size)  # slice

      # Discriminator
      if hps.model.mrd or hps.model.msstftd:
        # Discriminator Loss
        disc_real, disc_fake = net_d(wave), net_d(y_hat.detach())
        with autocast(enabled=False):
          loss_disc, losses_disc_r, losses_disc_g = discriminator_loss([x[0] for x in disc_real], [x[0] for x in disc_fake])

      else:
        y_d_hat_r, y_d_hat_g, _, _ = net_d.forward_org(wave, y_hat.detach())
        with autocast(enabled=False):
          loss_disc, losses_disc_r, losses_disc_g = discriminator_loss(y_d_hat_r, y_d_hat_g)

    optim_d.zero_grad()
    scaler.scale(loss_disc).backward()
    scaler.unscale_(optim_d)
    grad_norm_d = commons.clip_grad_value_(net_d.parameters(), None)
    scaler.step(optim_d)

    with autocast(enabled=hps.train.fp16_run):
      loss_stft = 0.

      # Generator
      if hps.model.mrd or hps.model.msstftd:
        disc_real, disc_fake = net_d(wave), net_d(y_hat)
        with autocast(enabled=False):
          # 1. Mel Loss
          loss_mel = F.l1_loss(y_mel, y_hat_mel) * hps.train.c_mel

          # 2. KL-divergense
          loss_kl = kl_loss(z_p, logs_q, m_p, logs_p, z_mask) * hps.train.c_kl

          if hps.model.mrstft:
            # new: Multi-Resolution STFT loss
            loss_sc, loss_mag = stft_criterion(y_hat.squeeze(1), wave.squeeze(1))
            loss_stft = (loss_sc + loss_mag) * hps.train.c_stft

          # 4. Feature loss
          # loss_fm = feature_loss(fmap_r, fmap_g)
          loss_fm = feature_loss([x[1] for x in disc_real], [x[1] for x in disc_fake])

          # 5. Generator loss (score loss)
          loss_gen, losses_gen = generator_loss([x[0] for x in disc_fake])

          # by default `loss_stft` is 0 unless `hps.model.mrstft` flag is set
          loss_gen_all = loss_kl + loss_mel + loss_stft + loss_fm + loss_gen

      else:
        y_d_hat_r, y_d_hat_g, fmap_r, fmap_g = net_d.forward_org(wave, y_hat)
        with autocast(enabled=False):
          if hps.model.mrstft:
            # new: Multi-Resolution STFT loss
            loss_sc, loss_mag = stft_criterion(y_hat.squeeze(1), wave.squeeze(1))
            loss_stft = (loss_sc + loss_mag) * hps.train.c_stft

          # original losses
          loss_mel = F.l1_loss(y_mel, y_hat_mel) * hps.train.c_mel
          loss_kl = kl_loss(z_p, logs_q, m_p, logs_p, z_mask) * hps.train.c_kl
          loss_fm = feature_loss(fmap_r, fmap_g)
          loss_gen, losses_gen = generator_loss(y_d_hat_g)

          # by default `loss_stft` is 0 unless `hps.model.mrstft` flag is set
          loss_gen_all = loss_gen + loss_fm + loss_mel + loss_kl + loss_stft

    optim_g.zero_grad()
    scaler.scale(loss_gen_all).backward()
    scaler.unscale_(optim_g)
    grad_norm_g = commons.clip_grad_value_(net_g.parameters(), None)
    scaler.step(optim_g)
    scaler.update()

    if GLOBAL_STEP % hps.train.log_interval == 0:
      lr = optim_g.param_groups[0]["lr"]

      scalar_dict = {
        "loss/g/total": loss_gen_all,
        "loss/d/total": loss_disc,
        "learning_rate": lr,
        "grad_norm/d": grad_norm_d,
        "grad_norm/g": grad_norm_g,
        "loss/g/fm": loss_fm,
        "loss/g/mel": loss_mel,
        "loss/g/kl": loss_kl,
        "loss/g/gen": loss_gen,
      }
      image_dict = {
        "slice/mel_org": misc_utils.plot_spectrogram_to_numpy(y_mel[0].data.cpu().numpy()),
        "slice/mel_gen": misc_utils.plot_spectrogram_to_numpy(y_hat_mel[0].data.cpu().numpy()),
        "all/mel": misc_utils.plot_spectrogram_to_numpy(mel[0].data.cpu().numpy()),
      }

      loss_msg = f"loss_disc={loss_disc:.3f} | loss_gen={loss_gen:.3f} | loss_fm={loss_fm:.3f} | loss_mel={loss_mel:.3f} | loss_kl={loss_kl:.3f}"
      if hps.model.mrstft:
        loss_msg = f'{loss_msg} | loss_stft={loss_stft:.3f}'
        scalar_dict["loss/g/stft"] = loss_stft

      misc_utils.summarize(writer=writer, global_step=GLOBAL_STEP, images=image_dict, scalars=scalar_dict)
      logger.info("[Epoch {} ({:.0f}%) | Step {}] {} | LR={} ".format(epoch, 100. * batch_idx / len(train_loader), GLOBAL_STEP, loss_msg, lr))

    GLOBAL_STEP += 1

  if epoch % hps.save_every_epoch == 0:
    if hps.if_latest == 0:
      misc_utils.save_checkpoint(net_g, optim_g, hps.train.learning_rate, epoch, os.path.join(hps.model_dir, "G_{}.pth".format(GLOBAL_STEP)))
      misc_utils.save_checkpoint(net_d, optim_d, hps.train.learning_rate, epoch, os.path.join(hps.model_dir, "D_{}.pth".format(GLOBAL_STEP)))
    else:
      misc_utils.save_checkpoint(net_g, optim_g, hps.train.learning_rate, epoch, os.path.join(hps.model_dir, "G_{}.pth".format(2333333)))
      misc_utils.save_checkpoint(net_d, optim_d, hps.train.learning_rate, epoch, os.path.join(hps.model_dir, "D_{}.pth".format(2333333)))

    if hps.save_every_weights:
      ckpt = net_g.state_dict()
      logger.info("saving ckpt %s_e%s:%s" % (hps.name,epoch,savee(ckpt, hps.sample_rate, hps.if_f0, hps.name + "_e%s_s%s" % (epoch, GLOBAL_STEP), epoch, hps.version, hps)))

  logger.info("====> Epoch: {} {}".format(epoch, epoch_recorder.record()))
  if epoch >= hps.total_epoch:
    logger.info("Training is done. The program is closed.")

    ckpt = net_g.state_dict()
    logger.info("saving final ckpt:%s" % savee(ckpt, hps.sample_rate, hps.if_f0, hps.name, epoch, hps.version, hps))
    sleep(1)
    os._exit(2333333)


if __name__ == "__main__":
  torch.multiprocessing.set_start_method("spawn")
  main()
