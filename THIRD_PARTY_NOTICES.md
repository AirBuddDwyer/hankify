# Third-party notices

Hankify's own code is under the MIT license in `LICENSE`. These parts are derived from other projects:

## RVC (Retrieval-based-Voice-Conversion-WebUI)
`windows/rvc-overlay/` holds modified copies of RVC files, and `patches/rvc-webui.patch` modifies RVC.
Upstream: https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI

```
MIT License

Copyright (c) 2023 liujing04
Copyright (c) 2023 源文雨
Copyright (c) 2023 Ftps

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Ultimate Vocal Remover (UVR5)
`patches/uvr.patch` modifies UVR (Linux setup only). UVR is MIT-licensed; its copyright notice is in its
LICENSE file: https://github.com/Anjok07/ultimatevocalremovergui/blob/master/LICENSE

## Used by Hankiplier, installed separately
Hankiplier's engine imports [OpenAI Whisper](https://github.com/openai/whisper) (MIT) and
[scikit-learn](https://github.com/scikit-learn/scikit-learn) (BSD-3-Clause), plus librosa (ISC) and SciPy (BSD-3-Clause),
installed into the RVC venv by the user. None of their code is copied into this repo.

## Downloaded at install time, not distributed here
The Windows installer downloads these from their official sources, under their own licenses: uv, Python, PyTorch,
the Python packages in `windows/requirements-windows.txt` (including python-audio-separator and
praat-parselmouth, which is GPL-3.0), RVC, FFmpeg (gyan.dev build, GPL), the RVC base models (HuBERT, RMVPE) and the
vocal separation models. No voice models are included or downloaded.
