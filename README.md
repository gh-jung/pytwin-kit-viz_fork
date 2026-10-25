# TwinViz — PyTwin TBROM + Omniverse Kit

PyTwin ROM을 별도 프로세스(`twin_worker`)에서 실행하고, 결과를 Omniverse Kit 110.1.4 앱(`kit-app`)에서
실시간으로 가시화합니다. 설계와 마일스톤은 [PLAN.md](PLAN.md)를 참고하세요.

## 구성
| 경로 | 내용 |
|---|---|
| `twin_worker/` | PyTwin 워커 (Python 3.12 venv, asyncio TCP 서버) |
| `kit-app/` | kit-app-template 110.1.4 기반 앱 `twinviz.app` + 확장 `twinviz.viewer` |
| `scripts/` | venv 설정, 프로토콜 동기화, 템플릿 replay 파일 |
| `models/` | `.twin` 및 메쉬 (git 제외) |

## 설정
```powershell
# 1) 워커 venv (Python 3.12 필요)
powershell -ExecutionPolicy Bypass -File scripts\setup_worker_venv.ps1

# 2) Kit 앱 빌드
cd kit-app
.\repo.bat build

# 3) 실행
.\repo.bat launch -n twinviz.app.kit
```

## USD 장면 로드

TwinViz Viewer의 **USD Scene**에서 경로를 입력하고 **Load USD**를 누르거나 **Browse**로
`.usd`, `.usda`, `.usdc`, `.usdz` 파일을 선택합니다. 기존 워커를 정지하고 새 스테이지로 엽니다.
스테이지의 모든 BasisCurves를 탐색해 `purpose = default`를 세션 레이어에 설정하고 개수를 표시합니다.
참조된 곡선과 비활성 곡선도 포함하며, 인스턴스 내부 곡선은 해당 인스턴스를 세션에서 풀어 수정합니다.
원본 USD 파일은 자동 저장하지 않습니다. 기존 `.twin` 모델 로드 기능도 그대로 사용할 수 있습니다.

## 테스트
```powershell
# 워커 단독
cd twin_worker; .\.venv\Scripts\python -m unittest discover -s tests -v

# Kit 확장 (워커 실행 → ping → 종료)
cd kit-app; .\repo.bat test
```

## 컨투어 / 등치면 / 스트림라인
확장 창 **Contours / Streamlines**: Levels(레벨 수), Banded colors, Iso-lines(표면, 메쉬 필요), Isosurfaces(개수),
Streamlines(시드 수, 벡터장만), Seed from(전체 또는 named selection). 내부를 보려면 Display의 Show를 `none`으로.
계산은 워커의 후처리 스레드에서 하며, 메쉬가 없으면 복셀 격자를 씁니다.

## 확장 설정 (`kit-app/source/extensions/twinviz.viewer/config/extension.toml`)
| 키 | 기본값 | 내용 |
|---|---|---|
| `worker_root` / `worker_python` | 빈 값 = `twin_worker/`와 그 `.venv` | 워커 위치 |
| `models_dir` | 빈 값 = `models/` | Load 목록에 보이는 `.twin` 폴더 |
| `model_units_in_meters` | `1.0` | TBROM 좌표 단위 |
| `write_backend` | `fabric` | 프레임 기록 경로 (`fabric` = USDRT, `usd`) |
| `max_display_points` | `1000000` | TBROM당 점 상한. 넘으면 워커가 무작위 표본만 계산·전송 (`0` = 무제한) |
| `debug_worker` | `false` | `true`면 디버거(`omni.kit.debug.python`)가 워커에 붙음 — 디버깅 전용, 느림 |

메쉬 표시는 [models/README.md](models/README.md)의 사이드카 파일 참고.

## 도구 스크립트
| 스크립트 | 용도 |
|---|---|
| `scripts/inspect_twin.py` | twin 입력/출력/TBROM 정보와 스텝 시간 출력 (워커 venv) |
| `scripts/kit_screenshot.py` | 앱을 띄워 모델 로드 → 창/뷰포트 캡처 → 종료 (`--exec`) |
| `scripts/kit_bench.py` | 프레임 반영 성능 측정, USD vs Fabric (`--exec`) |
| `scripts/kit_load_bench.py` + `scripts/fake_worker.py` | 대형 모델(합성 데이터) 로드·스트리밍 중 메인 스레드 멈춤 측정 (`--exec`) |
| `scripts/kit_worker_timing.py` | 앱 기준 로드 시간, 입력 변경→프레임 지연, 워커 스텝 속도 (`--exec`) |
| `scripts/field_check.py` | 모델별 한 줄 점검 코드 (워커 venv). 폐쇄망 절차: [docs/CLOSED_NETWORK_TEST.md](docs/CLOSED_NETWORK_TEST.md) |

## 프로토콜 변경 시
`twin_worker/twin_worker/protocol.py`가 원본입니다. 수정 후 `scripts\sync_protocol.ps1`로 확장에 복사하세요.
