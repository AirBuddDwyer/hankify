"""CI only: write a randomly initialised RVC v2 (40k, f0) voice model, so the Windows test can run the whole
pipeline without shipping a real (copyrighted) voice. It sings noise; the point is that every stage runs.
    python dummy_voice.py <RVC dir> <out.pth>"""
import os
import sys

import torch

rvc_dir, out = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
os.chdir(rvc_dir)
sys.path.insert(0, rvc_dir)
from infer.module.models import SynthesizerTrnMs768NSFsid  # noqa: E402

# Same config list a real RVC v2 40k export carries (checked against a trained model).
config = [1025, 32, 192, 192, 768, 2, 6, 3, 0, "1", [3, 7, 11], [[1, 3, 5], [1, 3, 5], [1, 3, 5]],
          [10, 10, 2, 2], 512, [16, 16, 4, 4], 109, 256, 40000]
torch.manual_seed(0)
net = SynthesizerTrnMs768NSFsid(*config, is_half=False)
weight = {k: v.half() for k, v in net.state_dict().items() if not k.startswith("enc_q")}
torch.save({"weight": weight, "config": config, "info": "CI dummy", "sr": "40k", "f0": 1, "version": "v2"}, out)
print(out, os.path.getsize(out))
