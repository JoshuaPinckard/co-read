# Private raw-transcript archive report

Generated at `2026-08-27T00:36:02.7468406Z` (local date 2026-08-26).

## Scope, privacy, and coverage caveats

This operation was read-only on `C:/Users/joshp/.claude/projects`. Raw transcript bytes were written only to the private sibling directory `C:/Users/joshp/Desktop/Blast-Radius-private/transcript-archive/`; they were not imported into this repository. The tar is local-only, **never published and never committed**.

Both repository manifests intentionally redact source paths. Reproducible entries were therefore mapped with the recorded fail-closed match rule: enumerate live `*.jsonl` paths by case-insensitive full-path order, scan forward, and accept a path only when SHA-256 of exactly the recorded prefix length equals that entry's recorded prefix hash. To satisfy the requested partial-recovery accounting, a failed ordinal was recorded without advancing the live-path cursor, allowing later entries to be tested by the same exact match rule. Missing entries retain their manifest ordinal, length, and hash, but their original paths cannot be recovered without a path-bearing manifest.

The build-params corpus is only partially reproducible. The requested oscillation manifest is no longer present: a later run overwrote the same path with a different freeze. No bytes were substituted in either case.

## Summary

| Freeze | Manifest identity | Prefix reproduction | Corpus SHA-256 verdict | Private tar |
|---|---|---|---|---|
| Build parameters | Found at `exploratory/build-params/corpus-manifest.json`; manifest SHA-256 `f7cc15116c91f68c26f4ac51f15952c68824882f0a72471c40fe3808a2102aa3`; records 6,290 entries, 4,151,991,960 bytes, and required digest `dcba21bb558eb1cc32dfc29a92b46817c2b8050d56c685bec7234ba2501c7458` | **6,137 reproduced / 153 failed**; 4,132,684,742 reproduced bytes / 19,307,218 unavailable bytes | **MISMATCH** — the complete corpus digest cannot be recomputed without the 153 missing prefixes. Diagnostic successful-entry framed digest: `07a99189bb1a11ac59e09778089d44fd735b84428a63c5f6a6db7ffaf579d92e` (not a complete-corpus digest) | `C:/Users/joshp/Desktop/Blast-Radius-private/transcript-archive/build-params.tar`; SHA-256 `db9d6a4f14074142290af1f9b6f011cdd95ad62945b986761ceeb09e95c66aa8`; 4,144,128,000 bytes |
| Oscillation | **Required manifest not found.** The original 5,943-entry / 3,995,044,226-byte freeze at `exploratory/oscillation/corpus-manifest.json` recorded `7cc5c524928d6e771d91f0351037cc72ae9284085f61ce0549742c89dacac5ac`, but that file was overwritten. The present file has manifest SHA-256 `9d35e0439d859a852cfe9583e569bc7486043bc630eb7872e64f103df695589e` and records a different 3,995,516,101-byte freeze with corpus digest `4dad985fafafdc45a787553a6234eccd13ec2a8821104b62a0111112a18a5aec` | **0 reproduced / 5,943 not attempted**; no per-file failure claims are possible without the required manifest | **MISMATCH (verification requirement unmet)** — no corpus digest was computed and the incompatible later manifest was not substituted | None created |

## Verification method

The recorded corpus digest rule is:

`SHA256(ASCII(ordinal) || NUL || ASCII(frozen_byte_length) || NUL || raw_frozen_prefix_bytes ...)`

with entries processed in ascending ordinal order. Each archived build-params member was written under its resolved corpus-relative path. The source manifest was included byte-for-byte as `corpus-manifest.json` at the tar root.

The completed tar was reopened and all 6,137 prefix members were read in full. Every member's size and SHA-256 matched its manifest entry; the archived manifest matched its source bytes; the tar contained exactly 6,138 members including the manifest. A separate filesystem hash pass reproduced the tar SHA-256 and byte size shown above.

The requested oscillation freeze is evidenced by the local Codex rollout record at `<current-user>/.codex/sessions/2026/08/24/rollout-2026-08-24T14-00-16-01a03592-e934-7ef2-95ec-e7e39d25a048.jsonl`: line 1005 records its count, byte total, timestamp `2026-08-24T22:07:16.699265Z`, and `7cc5...` digest; lines 988, 1082, 1224, and 1239 show that the manifest was path-redacted and later overwritten at the same output path. No raw rollout content was copied into this repository.

## Build-params per-file failures

Every row below is one failed manifest entry. `NO_ORDERED_PREFIX_MATCH` means no live candidate at or after the recorded resolver cursor reproduced the exact length-limited prefix hash. Because the manifest redacts paths, this result cannot distinguish disappearance, shrinkage, prefix mutation, or reordering, and the original relative path cannot be stated without fabrication.

| Ordinal | Frozen bytes | Expected prefix SHA-256 | Original relative path | Failure |
|---:|---:|---|---|---|
| 264 | 12262 | `eb2826a441f47a849817fc0d328d322a4204261faacf1dbe9016eff89e8c4f6f` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 265 | 12262 | `d13fb1fe1aa6aa8d3ebb910ade5c345898544bd4e1bd9e1766fdc5cccd0bbe2f` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 272 | 12254 | `c186e051cd228aee6b22f299c03fa2d3eac0b536920aac025220028dee20ae84` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 274 | 12262 | `7b56289e5b9f61a553e030063ce458a0cf6d480020cb4867fc92ac525be8886d` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5643 | 7131758 | `af58948fe490280300e2137d87bebc84c17e9c87fc73fdfad8062200f98a0fc3` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5644 | 311545 | `cf1de98c05c7e4513cab1a415c05bd465fae3674bdc78f8316ba5976f43feff4` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5645 | 96742 | `096aecf0a538175908e5290e57b0f8bf4f531b7e0b3bc0397d81df71f3d0fe4f` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5646 | 444040 | `d5c83bd5802c933f551851076a225ab9b7450dcdcaa166351b88bd58be0ad45c` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5647 | 102269 | `a188c4a75fa78f01bf06e8001c93bef80e898b3171836fe3dd6c7b6321c4016e` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5648 | 103700 | `5df3966bba7123bdc4a337764febfb4cb50e6804519708007425cb0580709171` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5649 | 16037 | `bce39211269bc94dc6696b11d4b242cb2912b13d9603bb7ed75c13fa536ef934` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5650 | 131402 | `4678f733858d5cd49319a3e94e475378b38278a5fab836abf31ed304b134d32d` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5651 | 117646 | `0eb1d50e7b6a30f82acd1f360f9943eac88a6f4803adfa54846562a3cc86a0ee` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5652 | 183916 | `411f7407598b7d11d0452f5baa5461cdbcd78f847b46de7f75d6c19219d6ef38` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5653 | 689015 | `fa554872ccc850651c5b3285bf36fc4a29dd8bfa11726601159155e7585a2b09` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5654 | 148602 | `2f7fbfe3beb6f02bb7ec1940f1465ff0b3beae90f9c9a0c385ff4d7423326c01` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5655 | 66950 | `ae5e77f166f4561a934f8dd2654431af9586e629ce37e00d6e05e65e08fedc6f` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5656 | 19941 | `f22469e17a63da63206544cf586d0fed64d629cc431c64a9ac094eae3e6a76c4` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5657 | 142568 | `5fb5c82b5d9edc44ff2ca74e4bda53d7cfa537d3ff3f632a200895eaaeeae7b5` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5658 | 58258 | `1ccd760a22043f6ae348428436512a753e81ca9e303278c75fe777c96b3d8d93` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5659 | 20419 | `efd3ce41142741e93fde497e25410fef0995cd7edcf68d8c8c4de42d02b0b897` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5660 | 23990 | `ec11468bbbcb377cc9a193092e07cf8b31e0372b8ef83e3daff32b5e9f1b0127` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5661 | 333312 | `b27f6bd33e34d240c9735e9495fbac8695e3b6404dd0dd8c68c978e20feb845c` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5662 | 362478 | `9cb274ba2d5ac78f366b859637a6c99e6bc4a610f6ad883d1629a3b61069cea5` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5663 | 190670 | `e5950f36e02d46ed2bcc4471ccf0ea5a9c8eca76a50649555669280b0371aca1` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5664 | 53451 | `df3d65b534d95db99a223601be7ab1c46ad2ff0aac447768213646b0e1b455bb` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5665 | 74771 | `71b2934c2b36297466d1bc3683b6deffed9f2ff832546b27a148f796f070fce8` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5666 | 46893 | `0a2a462147ca54780f187e6bde8823a5dc320dc7c758a963b7a2829aa1330def` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5667 | 56571 | `469be6ce2fbf55f611637a695d1d00b4454a03c4e66ed56c8e43be9753038ca9` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5668 | 80262 | `f6d46d900e8aed1c0d1ae1b7f989f223d7f51f00c86c684e5243e0e8d239a24a` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5669 | 86791 | `34a8d4350cb640c2113ea42e28ee45074f35c9f57b7e0491c902435dc7879bdc` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5670 | 74806 | `10f222e46082aec7ebbb77a53e9bc78a524318474e2fef80e2374578dedd94b0` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5671 | 71757 | `0a11bf4c134b1fced9659dbbe51fe1c0f887efe12813bf23c4db4fd3813368ef` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5672 | 66138 | `da2f9a473d15cfc0c18e2bd36620f4a42e97fd4849b4682715669bf773673396` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5673 | 59614 | `8207d93765a877089414ca448b9ec8a378cdd98a93221eb0c4b9dee621c3fa2a` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5674 | 35721 | `bdaccab7df52d2b6a24020fdd714e06f90bcb0e88d56a1143c2ab29fc802bc60` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5675 | 57928 | `e660d8e5a8d5268322b0cbb6cf512b7ae5ef831baf586f4974bb3cc3686f4b0d` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5676 | 37848 | `a9d241e449cf145f62c14e8cad14bd360894937f8ade7c48814500e8c4204f50` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5677 | 56171 | `68f7980ca05cbb97f3f6e6eaf57338f56f8e92fc56873147416e3349e15c4c5f` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5678 | 67571 | `ac0651d828a527bafdf1c5e6460f1974ae371c89c63502e5b0391aad5ed9d23d` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5679 | 34972 | `51c924a2c9510b752f9202cd76c74004d3f24d0b70a5497e3283b7d4aaa54adc` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5680 | 58978 | `15eef8adacd47c3d2a06d27065dc7e12de2b9d6b87c7ade66482ab75d971e7fc` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5681 | 34748 | `d086f0a6b0c8f17ee68b409e0821476f43886e7befd7d13665d279a3e4ed0a06` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5682 | 58946 | `9b1dde6f78fb0ad4736053022e89202bfc4d96147fba49a71d0978113e4aaa6d` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5683 | 74087 | `ed82b36d1d588703d0f483c506f003b225687ebc99c25047a4e0a598caf377d6` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5684 | 58144 | `5b3403d2435e1b4484d582a165dc12c78f541d0518bc413eac1e16304f4b9aa9` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5685 | 66432 | `2d7000575c403154fecf9bf49434dc6d65554c3220ff14cc02a44f21680ad611` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5686 | 66632 | `2a8bdcb2708d97b73ac7ba23e37eddcc973750971ea32de39effb13655d99c80` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5687 | 33685 | `1e354d2462f277da59d3c35fc439bfeb2cc82750ba6b5467c398e73980ba2336` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5688 | 71523 | `6899b21eb37f7623d3493d0634d36f3d50c05615749a98f700f0131b04cd81b4` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5689 | 59590 | `f4ed3e8c0e5dc713e133e2e5321288602b53c815e71ca36d098637b071670637` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5690 | 80347 | `d4aa8a84470d5e2a69f3a2400ebd9a24087bd4c1bde848931956abc4e9431ae9` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5691 | 37512 | `1edad36f3cbbccd3972669f28c963e6b10161953ca320c27aa6c58182db0cdbf` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5692 | 64019 | `9c4ca50d6c0fc2825606750898b50ba4b4dd89f37f344d389db990c442935496` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5693 | 82165 | `e36304320aa6f2aa64bf3f71474f9cd036c642c0fee7855b1db864628da149eb` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5694 | 57949 | `7e863023b41b248a2d6eaf4cee7ef50bb115474b7de458631dc1f49590bac2c7` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5695 | 67078 | `1c79fbd1967c6900fb94763675e9ca80e8b518b944c0552f1d6d0880a7909a15` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5696 | 59911 | `6ca2d9eb382d1488792c8da2b0316547b459c70b3bbe18a95494ef0abde54e64` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5697 | 55076 | `24c49904667e8d225b5bab66114b8a1cc941b7384b3639eed80cc2dc0660b5d3` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5698 | 60220 | `dab00b25d7984d45868be8b8ceefc02c4c5b7815900d62ca6ab62d7cbcaf2d4a` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5699 | 69368 | `e3cf7cbf7fc7c534eb02ff9f60304c434e6fb7d3f92baa558611eb1469d039ae` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5700 | 37756 | `33ef564de071d860565acb52efb1b4edb256bd4e3176ce67d0b3a254401a39ec` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5701 | 58475 | `067ff537167696fbc39fba8c88df8c6f21b8e6e5b549b90dcc2577ab3d0fad34` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5702 | 72381 | `f9c58d4b673ae87d232c10e9d2bb2cf812d46815da502f3ae60ebd3355282514` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5703 | 41794 | `5f1b71d251e4f4cd9e65a6a5e534cd4b65a75164eda4c9395862b7cc29f68535` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5704 | 49804 | `253cd2b58d1aeb4e30fff5bb91f0cd0cec933c5e117504135ec201bb11563640` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5705 | 67378 | `fa3bfebd02cf310850f62db5a9c4fc8a1f80d6552b2195086f73c2f34251b37e` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5706 | 106071 | `8290723474c7e59e7e358d37546b11511e7ace7c8512806848ef37de3a47a41b` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5707 | 67725 | `c0d0a41863d0e103b47c142c24c76190e68457978ff11c8f1cc99e9f98627361` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5708 | 63825 | `1499641fef405c6cee8b68244f0dd7762f2c0e3f86eaa4ebc7c849ff82d47754` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5709 | 60855 | `7755736a472f6d8d13a7c26caf97394c6b5dc298a3e7dfab56ec6ae48a47eb1f` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5710 | 60811 | `2325ea413bec5024e6f54a8700f39303ee5b6fc015e16ed9acc7fffc2a7b54e6` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5711 | 64533 | `dcf3722f57f6e839f0ff33cd21b3ef5104cfb6f23e32d7f5a02f3d2a5f20ff77` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5712 | 35370 | `2e2847d214f3edfcddc4c917d3a2a87de5e3bd143e5d6bf5241d3de3d9361078` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5713 | 48121 | `5fe09b465b51f2a141c5cf3f72cabac9d128dbd3ebd3f4462ec4759753544485` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5714 | 74558 | `21bf9d2d23b884d7e575c608b6451c28d9eac3eab9e9bb12cf29753b967c9f4b` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5715 | 37747 | `ce13fb6ebf90d007c64a9e9a914f3f20800000a3410e045ea3f0b02442e0ae2a` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5716 | 62037 | `e4f34bbf6ab5b677fe43cc238fe61a056721adeb744757f8597651aea65b5c09` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5717 | 50957 | `9a6576c749ba192353a7f266e31dd481590a90a78539c8401960e0a4f78b107c` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5718 | 78512 | `d58fbebd4202e0fd95f74db355647a377cd2b58c60e28c6b95e6a54673544e28` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5719 | 48760 | `88e50a0ff0197b8b82f7e51768f9344ff708091ac696cf70e33282f8f42cc402` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5720 | 67376 | `c8bf278b7e7b158eb6b0a9d39e076678ba5df2d2039646c1141ffb5a55f10c87` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5721 | 38442 | `9dedf88e875f43206af04f6eafa56727c00b19f2de43cbf78f82264e82bda1c3` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5722 | 61555 | `f7edef04bda7ffd368e950cf79f59b75a8c74acacf7a99ce0915621bdc839a4b` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5723 | 46636 | `4d256bae3eeec159e986429fe15925c8474287104dde70233522eacd8d5a3665` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5724 | 38162 | `0eca225cd68ecbc7add71fe01c31a0f7dd4f5f645d7d81e4132b0c16ac538a7c` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5725 | 36528 | `63fa92a138daba8b48b76b5b7d5957ef023a5185de52139fa22ee78a0bae7b08` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5726 | 67913 | `b6c80a098d18f2f62a0678a0d9ef0316e1ca6e5d9d0ada36a1de10a4caea514f` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5727 | 53335 | `6a76ed7fe4ee0afc2f2b2f71a53283d9c260fdb953ede29d641e28b4f6f2a8e1` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5728 | 57787 | `874ef11f3f9c92856f8f6f74673de8377ed9b456d3fbfe5da4b24fd0c685c1af` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5729 | 59655 | `fb9143ae7ec35cec404ff80cd809ebbeb970c50d0dcb18899cb3ad292c8f6c51` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5730 | 64602 | `3aaee3e711739e816c10c995c7c20856e6982fe6b0d8c667ad881bc9d1cc3ab7` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5731 | 71973 | `4ad759093efc84ff8e2e6155c67d74bece7544ff1b58a1827b1b4e15e825a2a4` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5732 | 33887 | `1b255717a198d11674289628ff95a85fdc60ea311438bb72e46d5fb53722624a` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5733 | 64558 | `ce03adbc0290b14788bae554a9d502760d24da883aa7ac1a7bdf2a1034390f42` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5734 | 52518 | `9f19426872f74b5433531833beb5e9c1b7bd28ba33c2c20fc828bab551935317` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5735 | 36186 | `abf82bd1a4b1e105ce41ae670da7af92426e8a749a173c20ef6521e1ef815790` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5736 | 59895 | `4d077ff8b49b8b427d56c86270d8576fe4c265862d5d35ccea2346e348450ce6` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5737 | 35714 | `af0d45c8190980120044a64b9f5673a844c014f348832d4e05cfebd2cd454d88` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5738 | 65469 | `06b9f8b4a05ed48aa5a0fa918b4d12f5287d453fc03c13ac1e25c91beb4d275a` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5739 | 83317 | `c8a4585aebee935615bdf2d7e8e2ba45e24d956a28e1a83990c3e51ef11c3b67` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5740 | 72417 | `0d01fc3a39248a8a46caf81bb961851887c387b850c49bfc76402cf25161d390` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5741 | 42258 | `bc31686cbe082eef380f8eb4832035157ba736da3ba32ace5c088f0b2d41cd19` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5742 | 48183 | `a20cb7645098243834bab4de584b68a3a934aca5b881bce77e6ffdbf34724cc0` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5743 | 83157 | `1448042e4bdc9d635e68a744e6ed96eadf25d9cfb2a6ee833eb6fad0bb832a4b` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5744 | 89942 | `25a74ba143c180ac0bc074b31932c863128d1557157e9ed24a43f0014ebc8339` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5745 | 51829 | `69b08758d682e919866359e57079626240bea8d2ba912bee906e4b459c8c8710` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5746 | 35128 | `258b6f3ac0e7b861765c632061052caacd8f6e15b29d73c52df52bef1b6f207c` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5747 | 67258 | `faa9a770b4a2078b835a68536d7102401749af8ed622acab05946e42780ec078` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5748 | 60388 | `c882e992d293eeea3e74f91a1261b268a6ec55b679bc0a52891d3e8497ffe474` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5749 | 60912 | `66105c60f7d4ffbd42de822b9eec08d46b33a0af6467a01c425dce585a56f4e9` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5750 | 55335 | `e491bcc0ea2fe955b6c49fa946fd3013a91543e27349882be6272f396357a83c` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5751 | 38150 | `ddd2fbd8c025eefefab554cdfc7297e69afbc629f89e57c9993d4bbcf175927a` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5752 | 67339 | `1f0fec506ff20fb0bb48115c2d82849ef3675580d9b5c4fcf0e154e22ec2f844` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5753 | 88193 | `48813d30326122cab3a790c4caafd18d8f22a79a6b733e7400d99e613b418839` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5754 | 59170 | `ab6954ee6c8c40ccebf9ca4b6465f6bac07db7591ec175048e63503ed97b7bc6` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5755 | 60586 | `03bdf6bea517e631baa8d6ce95b77c617dec09da5b41e4dcfa22c1a1bb98125a` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5756 | 65410 | `567f867ed54e759fea71ee8b610aed340025dea7a2dee17cc3dcec812693b79a` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5757 | 49921 | `4d3325c670d4f190199dda86c4d756e08559551dc0e3cd46e133d624486af729` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5758 | 35019 | `58de153aaa8b5400914e7d7758853ff8a556eba7bed5683717a527083222e3b5` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5759 | 74776 | `502eed01966cd8f5b3c3d816214582334089a5e879569759957bf91bc7116a60` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5760 | 75440 | `1a3d1907af997c05c29668cc3477972b04d93980a5f5afd157eba3d61817e777` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5761 | 65930 | `d59a0ffe77a45df08e0ca36f059bf79126d7563d185b70d80e014223d31494c8` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5762 | 65841 | `d783ed3b8f6a1799997e358e18f718d5a0fcec0da131e9570bdfc5861d5b19c7` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5763 | 56111 | `527e9aea71c39904a74745f89da8a2c49c82b64acdcf964cb3c928af5ff7e498` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5764 | 41516 | `898ba35f4f0f4a618e8af09b7e93bf43ec05f2536189b6432417af8f4c45eef1` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5765 | 77535 | `196f023edebb0a564911fe51b69c055f86be398061f06fa4f0590ee3b36faf9f` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5766 | 98848 | `19aede8de132781b3d53ccd6567570acd6c99eedcdca73d473c60a8d1509e812` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5767 | 63763 | `0b2489525d7860bcb6d3a9696049d17496bb371b7228f2c3a39f9d86e62f96b1` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5768 | 67110 | `2677522d88502f82e8279e21a42d65325d89625c7763e4f4b72af34dc726da13` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5769 | 38063 | `343f98135eddebd8392749714c7c73a488e00af7b1950a5b305fc6a6cb6bf7ca` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5770 | 59950 | `54bb9615f78bdb7708052a160ad5e793fc5cc8e6553aea5f5e5458e0a0ded1cd` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5771 | 35884 | `b6089bd5e3ba18d00c010f475edeea828c955ba1b504d0fbe1fb7bb328d7fe02` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5772 | 37012 | `c609dd9cd7aae16d0b57eee645b72d8b5d6e97ec83a0d4d6c91cb2fd9b5c30fb` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5773 | 72061 | `846843e538718cf846a806ba342c099adbf55e51d925c84f267f85c043c97b04` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5774 | 67373 | `2e6a89c6032c612eb318d06d5ab5ddcdd9758b444b203d16a3d684888d1bcc90` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5775 | 251704 | `df82917ef734fba36dfcda5e8e4b6b314da15080dfff7ca2d505d1b58d3a09bb` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5776 | 150303 | `689e868294caef546fbc3cb074e53cc060b0a835b71e75faaf31d77b1f60500a` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5777 | 89040 | `44c86f5a2bfe9355ca37bb6c7b5eb26208ec08f2a65c432ab658016a16332eb7` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5778 | 127492 | `4d0455672ccadd183e34c7d4abf380fab5d329535f83fab295c3b842d7fdac40` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5779 | 147323 | `af6cd2485e97edb0c60904180be265b76e6d2c24f7c25d4d7bbc9f4287e6c7e3` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5780 | 48487 | `28c9f24ee002ce5d3480e57b55283e42d5fe3e5c769d9a88008c6f6555d4e9f1` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5781 | 79634 | `3ae4576a9c4e4eb6cc252ee814bacdf422d4c793607e3f4ebfbc8bb019640aa1` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5782 | 76770 | `a52d9c79c5f89da2cf13652253f1651b2b152cd7945746826843ed3f1b2593b0` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5783 | 165933 | `f5ddf2b7b881055c1e0046005f6815f68351d9b965f31bbc4066bac05ee982b0` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5784 | 170870 | `6c30ab2920f0d4efa996727d40d6077d8939d26285c234a000dfa021bf550d20` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5785 | 168492 | `8d35608741ebe777ce9e22129d1a5d95f81b5432204a4ff33b514b497cf54dae` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5786 | 83441 | `70c2319916af3ffbefe28e83390654daf672e2ecb30bc4753f87b774da5bdf13` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5787 | 7934 | `4480c859d55c2f40bbd29f123f5783bf4e013e0c3c6b1fb2db61db6cba788482` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5788 | 159603 | `1ce9dd0f5c30bda555e2ad64b70240d50e2fa4305b7602e768b1abd7824ed550` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5789 | 139212 | `72ac78aa8c9674958527363cb101d0ec9f4acb6ad129e98eadcdd421afe9641d` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5790 | 107846 | `6327e1b0d3a08387d59fa9f8b2716ed862a6ab5f609b0350c729f6f272a62029` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |
| 5791 | 375 | `53f15cf5f77dbd4806adb0e3d3151f7bc8919aa75cbb9c34df0fdc71cc7e3379` | *Unrecoverable: recorded manifest redacts paths* | `NO_ORDERED_PREFIX_MATCH` |

## Oscillation per-file failures

None were asserted. The required per-file manifest was overwritten and could not be located, so all 5,943 entries were left unattempted and no oscillation tar was created. Treating the later `4dad...` manifest as the requested `7cc5...` freeze would be an approximation and was explicitly rejected.

## What I got wrong

| Earlier working belief | Verdict |
|---|---|
| A bounded lookahead around the first build-params gap suggested only ordinals 264 and 265 were absent. | **Incomplete.** The full partial-recovery pass using the recorded exact-match rule found 153 failures: ordinals 264, 265, 272, 274, and 5643–5791. The archive and counts use the full result. |
| The visible 5,943-entry oscillation manifest might be the requested freeze with a differently described digest. | **Wrong.** The local run record establishes two distinct freezes written to the same path; the later `4dad...` manifest overwrote the requested `7cc5...` manifest. |

## Claims that could NOT be verified

- The original relative paths or exact failure mode of the 153 unavailable build-params entries. The retained manifest deliberately omits paths.
- The complete build-params corpus digest. Omitting failed bytes cannot yield the recorded whole-corpus digest, and no bytes were substituted.
- Any per-file property or live reproducibility count for the requested `7cc5...` oscillation freeze. Its per-file manifest is unavailable.
- That no copy of the overwritten oscillation manifest exists outside the repository, Git history, relevant local run records, and searched local artifacts. No global absence claim is made.

## What would change this verdict

- Restoring live files whose ordered frozen prefixes match all 153 missing build-params entries would permit a complete rebuild and a direct test against `dcba21bb...`.
- Recovering a byte-identical copy or backup of the original oscillation per-file manifest would permit exact prefix reproduction and a direct test against `7cc5c524...`.
- A path-bearing contemporaneous manifest would make original-path and disappearance-versus-mutation claims testable.

## Confidence by claim

| Claim | Confidence | Reason |
|---|---|---|
| The private build-params tar contains 6,137 exact frozen prefixes plus the byte-identical manifest. | High | Full tar re-read verified every included member's byte length and prefix SHA-256, manifest bytes, and member count. |
| The build-params tar SHA-256 is `db9d6a4f...c66aa8` and its size is 4,144,128,000 bytes. | High | The creation verifier and an independent filesystem hash/size pass agreed. |
| The build-params complete corpus does not match the required reproducibility condition. | High | Exactly 153 entries / 19,307,218 bytes are unavailable; a complete digest cannot be computed without substitution. |
| The failure identities, lengths, and expected hashes in the table are exact. | High | They are read directly from the frozen manifest and aligned against the verified tar's ordered members. |
| The failed build-params files vanished, shrank, mutated, or reordered. | Not verified | The path-redacted manifest makes those causes observationally indistinguishable. |
| The requested oscillation manifest was overwritten and no oscillation tar was created. | High | The run record identifies both writes and distinct freeze hashes; the target tar path was checked absent. |
| The requested oscillation corpus itself is unreproducible from every possible backup. | Not claimed | Unsearched external or offline backups may exist. |
