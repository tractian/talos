# Third-party licenses

## Kev

https://github.com/jaredpalmer/kev

Copyright 2026 Jared Palmer

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Talos-0.8B was trained with Kev's training and calibration recipe. The gate speaks Kev's `/v1/systemone` HTTP interface. No Kev source code is included in this repository.

## Qwen3.5-0.8B-Base

https://huggingface.co/Qwen/Qwen3.5-0.8B-Base

Copyright Alibaba Cloud. Licensed under the Apache License, Version 2.0 (full text in `LICENSE`). The Talos-0.8B weights are a LoRA adapter and pointer head trained on this base model.

## Runtime dependencies

pydantic (MIT), numpy (BSD-3-Clause), orjson (Apache-2.0 OR MIT), PyYAML (MIT), onnxruntime (MIT), httpx (BSD-3-Clause), pymodbus (BSD-3-Clause). Each is installed from PyPI under its own license and is not bundled here.
