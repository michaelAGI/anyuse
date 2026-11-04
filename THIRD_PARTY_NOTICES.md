# Third-party notices

The project's own code uses the [MIT licence](LICENSE). Existing third-party
material retains its original terms. Installing a dependency does not make it
part of this project's MIT licence.

## Bundled EasyList/EasyPrivacy domain extract

File: `execution/web_scope_data/easylist_domains.txt.gz`.

Attribution: **The EasyList authors (<https://easylist.to/>)**.

This is a transformed domain-only extract, fetched on 25 September 2026 from
the [EasyList repository](https://github.com/easylist/easylist) at commit
`5ab489cbbf53b903be53e621c0b4f5fab8d98049`. It is not the original filter-rule
files. The extraction removes rule syntax and path information, retains a
domain list and compresses that list. Its recorded source files are:

- `easylist/easylist_adservers.txt`
- `easylist/easylist_thirdparty.txt`
- `easyprivacy/easyprivacy_trackingservers.txt`
- `easyprivacy/easyprivacy_trackingservers_general.txt`
- `easyprivacy/easyprivacy_trackingservers_thirdparty.txt`
- `easyprivacy/easyprivacy_trackingservers_international.txt`
- `easyprivacy/easyprivacy_thirdparty.txt`

The recorded uncompressed extract contains 57,066 domains and has SHA-256
`f64d1a2992b7a78787d7a40fb4c1e814a7daca09f2a01a88a38fe9a45490ac4d`.
The runtime verifies that hash. See [the data record](docs/TRAFFIC_POLICY.md)
for the extraction's use and limitations.

The extract retains the upstream choice of
[GPL version 3 or later](https://www.gnu.org/licenses/gpl-3.0.html) or
[CC BY-SA 3.0 Unported](https://creativecommons.org/licenses/by-sa/3.0/),
with the upstream option of a later applicable version. See the
[EasyList licence statement](https://easylist.to/pages/licence.html).
The project's MIT grant does not replace these terms for the extract.

Complete copies accompany the package:
[GPLv3](licenses/EasyList-GPL-3.0.txt) and
[CC BY-SA 3.0](licenses/EasyList-CC-BY-SA-3.0.txt).

Package metadata therefore records the combined expression
`MIT AND (GPL-3.0-or-later OR CC-BY-SA-3.0)`: MIT for the project's own code,
plus the recipient's licence choice for this separately identified data.

## Analysis tools, optional decision models and target applications

REA, Ghidra, Frida and target applications retain their respective terms. The
recorded research kept installed tools and original app binaries inside the
VM; they are not bundled by adding this project's MIT licence.

[Laya](https://huggingface.co/convaiinnovations/laya) ([source](https://github.com/NandhaKishorM/laya),
`pip install laya`) by Nandakishor M / Convai Innovations is licensed under
[Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0). Its weights and
wheels are not bundled in this repository or wheel; `tools/confirmer/fetch_laya.py`
downloads them on demand from Hugging Face (`convaiinnovations/laya`,
`convaiinnovations/laya-typed-decisions`, `convaiinnovations/laya-multilingual`)
and PyPI under their original licence terms.

This notice identifies known bundled data and external tools and is not a
complete dependency licence audit.
