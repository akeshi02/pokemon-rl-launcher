# Credits and attribution

## Original project

This is an unofficial add-on for **PokemonRedExperiments**, created by
**Peter Whidden**:

- Repository: <https://github.com/PWhiddy/PokemonRedExperiments>
- Author: Peter Whidden
- License: MIT

The emulator environment, the training loop, the reward design and the
`baseline_fast_v2.py` training script are his work. This project only adds a
launcher, a CLI entry point, environment-detection helpers and extra reward
shaping — and it downloads his project from GitHub at install time instead of
redistributing it.

If you use this project, please credit the original work:

> Peter Whidden, *PokemonRedExperiments* (MIT),
> <https://github.com/PWhiddy/PokemonRedExperiments>

## The one file derived from upstream

Everything in `overlay/` is new code except one file:

| File | Origin |
|---|---|
| `overlay/v2/tensorboard_callback.py` | A copy of upstream's `v2/tensorboard_callback.py` with a single fix: the explore-map image no longer hardcodes 2 rows, which raised `einops.EinopsError` whenever the number of environments was odd (e.g. `num_cpu=1`). The change is marked with a comment in the file. |

The MIT license permits this. The upstream copyright notice applies to that
file and is reproduced at the bottom of this document.

## Third-party software

Used at runtime or bundled into the built executables, each under its own
license:

| Project | License |
|---|---|
| [PyBoy](https://github.com/Baekalfen/PyBoy) | MIT |
| [stable-baselines3](https://github.com/DLR-RM/stable-baselines3) | MIT |
| [Gym](https://github.com/openai/gym) / [Gymnasium](https://github.com/Farama-Foundation/Gymnasium) | MIT |
| [PyTorch](https://pytorch.org) | BSD-3-Clause |
| [einops](https://github.com/arogozhnikov/einops) | MIT |
| [TensorBoard](https://github.com/tensorflow/tensorboard) | Apache-2.0 |
| [customtkinter](https://github.com/TomSchimansky/CustomTkinter) | MIT |
| [matplotlib](https://matplotlib.org) | PSF-based |
| [psutil](https://github.com/giampaolo/psutil) | BSD-3-Clause |
| [pywin32](https://github.com/mhammond/pywin32) | PSF |
| [PyInstaller](https://pyinstaller.org) | GPL-2.0 **with a special exception** that explicitly allows distributing the executables it produces, so `PokemonRLLauncher.exe` / `PokemonRLSetup.exe` are not GPL-encumbered |

## What is not included

- **No ROM.** Pokémon Red is © Nintendo / Creatures Inc. / GAME FREAK inc.
  The installer never downloads or links to one — you must own the cartridge
  and dump it yourself.
- No save states, trained models, or training output.

## Trademarks

Pokémon and Pokémon Red are trademarks of Nintendo, Creatures Inc. and GAME
FREAK inc. This project is unofficial and non-commercial, and is not
affiliated with or endorsed by them. The project logo is an original drawing
(a stylised Poké Ball with a neural-network motif), not an official asset.

---

## Upstream MIT license — PokemonRedExperiments

Copyright (c) 2023 Peter Whidden

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
