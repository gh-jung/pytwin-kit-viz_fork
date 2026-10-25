# TwinViz Viewer [twinviz.viewer]

PyTwin의 TBROM 결과를 Omniverse Kit에서 시각화하고, 일반 USD 장면을 로드하는 Python 확장입니다.
저장소: [gh-jung/pytwin-kit-viz_fork](https://github.com/gh-jung/pytwin-kit-viz_fork).

## 1. 역할과 범위

이 확장은 사용자 입력, 워커 프로세스 제어, TCP 결과 수신, USD/Fabric 기록을 담당합니다.
실제 PyTwin 계산과 VTK 기반 후처리는 저장소 루트의 `twin_worker/`가 담당합니다.

두 로드 경로가 있습니다.

- **Model → Load**: `.twin` 모델을 워커에서 계산하고 결과를 현재 스테이지의 `/World/TwinViz`에 표시합니다.
- **USD Scene → Load USD / Browse**: 워커를 정지하고 USD를 새 스테이지로 엽니다. 모든 탐색된 BasisCurves의 `purpose`를 `default`로 설정합니다.

일반 USD 로드는 PyTwin 계산을 거치지 않습니다. USD에 들어 있는 애니메이션을 PyTwin의 Run 버튼으로 진행시키는 기능도 아닙니다.

## 2. 디렉터리와 코드 지도

```text
twinviz.viewer/
├─ config/extension.toml       확장 메타데이터·의존성·기본 설정
├─ premake5.lua               빌드 대상과 런타임 폴더 링크
├─ data/                     Extension Manager 아이콘·미리보기 PNG
├─ docs/                     이 문서·개요·변경 기록
└─ twinviz/viewer/
   ├─ __init__.py            Kit이 로드하는 패키지 진입점
   ├─ extension.py           확장 시작/종료·설정·기본 경로 탐색
   ├─ window.py              omni.ui 패널·사용자 액션·USD 열기
   ├─ session.py             워커/통신/화면 상태와 명령 중계
   ├─ process_manager.py     워커 subprocess·준비 감지·로그
   ├─ client.py              TCP 연결·수신 스레드·메인 스레드 dispatch
   ├─ protocol.py            JSON 헤더와 binary 배열 전송 규약
   ├─ stage_updater.py       ROM별 장면·배열 준비·USD/Fabric 커밋
   ├─ colormap.py            벡터 크기·팔레트·sRGB→linear 변환
   ├─ overlays.py            등치선/등치면/스트림라인 표시
   ├─ usd_loader.py          BasisCurves 탐색과 purpose override
   └─ tests/                 워커·표시 통합 테스트 및 USD 단위 테스트
```

`data/*.png`는 표시 자산이며 코드 주석 대상이 아닙니다. `__pycache__/`는 Python이 만드는 캐시입니다.

## 3. 확장 시작과 종료

```text
config/extension.toml의 python.module
  → twinviz.viewer.__init__
  → TwinVizExtension.on_startup(ext_id)
      → 설정/워커 Python/모델 폴더 경로 결정
      → TwinSession 생성
          → WorkerProcess + TwinClient + StageUpdater 생성
      → TwinVizWindow 생성 및 Property 옆에 도킹
```

설정이 비어 있으면 `extension.py`가 확장 실제 경로의 상위를 탐색해 `twin_worker/pyproject.toml`을 찾습니다.
기본 실행 파일은 `twin_worker/.venv/Scripts/python.exe`, 모델 폴더는 그 상위의 `models/`입니다.
확장 시작만으로 워커 프로세스를 실행하지는 않습니다. Start Worker 또는 모델 Load 시 시작합니다.

종료 시에는 창을 정리한 뒤 세션을 정리합니다. 세션은 복구 작업 취소, 워커 종료 요청,
소켓/이벤트 구독 정리, 프로세스 종료 대기, 표시 prim과 준비 작업 정리를 수행합니다.
Kit 종료 중에는 이벤트 루프가 다시 실행되지 않을 수 있어 `destroy()`는 await 없이 정리합니다.

## 4. .twin 로드부터 첫 화면까지

```text
window._on_load()
  → session.load(path, meshes)
      → start_worker()
          → WorkerProcess.start(): python -m twin_worker --port 0 --parent-pid ...
          → stdout의 TWIN_WORKER_READY port=N 대기
          → TwinClient.connect(N): hello/프로토콜 버전 확인
      → 이전 모델 표시와 메타데이터 초기화
      → load {path, max_points, meshes?} 전송
      → set_post {settings} 전송

워커 결과
  model_info → 입력/출력 UI 구성
  geometry   → ROM별 TBROM 원본 좌표 등록
  mesh_map   → 메쉬 정점/삼각형/매핑 인덱스 등록
  frame      → 필드 값으로 색상/변형 준비 → 메인 스레드 커밋
  post       → 등치선/등치면/스트림라인 prim 갱신
```

`await session.load()`의 완료는 **명령 전송 완료**입니다. 모델 로드나 첫 화면 표시 완료를 보장하지 않습니다.
스크립트에서 실제 표시를 기다릴 때는 `updater.views`, `frames_applied`, `settled` 등 목적에 맞는 상태를 확인합니다.

메쉬는 워커가 TBROM 점에 대응시킨 인덱스를 보냅니다. 뷰어는 매 프레임 최근접 탐색을 하지 않고 인덱스로 값을 가져옵니다.
미매핑 인덱스 `-1`은 회색으로 표시하고 변형을 적용하지 않습니다. 메쉬가 없으면 points 표현으로 대체합니다.

## 5. 스레드와 최신 프레임 처리

| 실행 위치 | 수행하는 일 | 장면 수정 |
|---|---|---|
| 워커 프로세스 | PyTwin 계산·메쉬 매핑·후처리 | Kit 스테이지에 접근하지 않음 |
| Kit 수신 스레드 | blocking socket 수신·binary를 NumPy 배열로 해석 | 없음 |
| Kit asyncio / 메인 스레드 | UI 액션·명령 전송·수신 핸들러·업데이트 이벤트 | 있음 |
| Kit 준비 스레드 1개 | 필드 색상·벡터 변형 배열 준비 | 없음 |
| stdout/stderr 읽기 스레드 | 워커 포트 감지·최근 로그 수집 | 없음 |

`TwinClient._read_loop()`는 일반 메시지를 순서 있는 큐에 넣고 `frame`, `post`는 종류별 최신 메시지만 유지합니다.
`pump()`는 Kit 업데이트마다 대기 메시지를 꺼내 세션 핸들러를 메인 스레드에서 호출합니다.
따라서 프레임이 빨리 도착하면 중간 결과를 버릴 수 있으며, 모든 시뮬레이션 스텝을 화면에 그리는 구조가 아닙니다.

`StageUpdater`도 pending 슬롯과 실행 중 준비 작업 하나를 사용합니다. `_submit()`은 표시 스타일을 캡처해
NumPy 준비 작업을 제출하고, 다음 업데이트에서 `_commit_finished_job()`이 결과를 반영합니다.
모델이 교체되면 generation 값이 바뀌어 이전 모델의 준비 결과는 버립니다.
스타일 버전은 마지막 반영 스타일과 비교해 `settled` 상태를 판정하는 데 사용합니다.

## 6. 렌더링 데이터 경로

### 기본 필드: stage_updater.py

- `_ArrayWriter`: 속성 배열을 Fabric 또는 USD에 쓰고 읽습니다. Fabric 기록 예외 시 해당 writer는 USD로 전환합니다.
- `_Layer`: 원본 점 또는 메쉬 한 개의 기준 좌표, 색상, 변형을 관리합니다.
- `FieldView`: ROM 하나의 points/mesh 표현과 표시 범위를 관리합니다. 필요한 points prim만 지연 생성합니다.
- `StageUpdater`: geometry/mesh/frame/post의 도착과 Kit 업데이트를 연결합니다.

스칼라는 값 자체, 벡터는 크기를 컬러맵 입력으로 사용합니다. 변형 배율이 있으면 3성분 벡터를 기준 좌표에 더합니다.
팔레트는 sRGB로 정의되지만 `displayColor`에는 선형 색을 기록합니다. 머티리얼은 색상과 발광 성분을 함께 사용합니다.
모델 좌표 단위는 `/World/TwinViz` 루트의 스케일로 스테이지 `metersPerUnit`에 맞춥니다.

기본 `write_backend = fabric`은 매 프레임 배열을 USDRT로 직접 기록합니다.
이 경우 USD 레이어에는 초기값만 남을 수 있으므로 **USD 저장만으로 현재 화면 결과를 보존한다고 가정하면 안 됩니다.**
테스트에서 실제 기록 값을 읽을 때도 `updater.read_attr()`로 사용하는 backend를 통해 읽습니다.

### 후처리 형상: overlays.py

등치선과 스트림라인은 `UsdGeom.BasisCurves`, 등치면은 `UsdGeom.Mesh`로 기록합니다.
곡선은 `points`와 곡선별 정점 수 `curveVertexCounts`로 여러 선을 한 prim에 묶습니다.
후처리 결과는 연결 구조가 바뀔 수 있어 Fabric 기본 필드 경로와 달리 USD를 통해 갱신합니다.
새 `post` 메시지에서 빠진 `(ROM, 종류)`의 기존 prim은 제거합니다.
후처리 계산 주기는 워커에서 제한하며, Kit 렌더링 FPS와 별개입니다.

## 7. 실행·입력·후처리 제어

| 세션 함수 | 의미 |
|---|---|
| `start(step_size, realtime, speed)` | 시간 진행 요청과 복구용 실행 인자 저장 |
| `pause()` | 실행 인자 제거 후 일시정지 요청 |
| `reset()` | `stop` 명령으로 워커 모델 재초기화 요청 |
| `set_input(name, value)` | 입력별 최신 대기값 저장 및 전송 작업 예약 |
| `set_post(**settings)` | 후처리 설정 저장, 연결 중이면 즉시 전송 |
| `ping()` | 왕복 통신 지연 측정 |

같은 슬라이더를 빠르게 움직이면 `_pending_inputs`의 해당 입력값을 덮어씁니다.
동적 모델은 전송 후 50ms를 기다리고, 정적 모델은 다음 프레임을 최대 30초 기다린 뒤 다음 입력을 보냅니다.
정적 모델의 완료 신호는 요청 ID 대조가 아니라 다음 frame 도착입니다.
동적 모델의 입력은 워커의 다음 계산 스텝에서 반영되므로 일시정지 중에는 즉시 필드가 바뀌지 않습니다.

`session.fps`는 세션 콜백에서 처리한 frame 빈도입니다. GPU FPS와 다르며 `updater.frames_applied`와도 차이가 날 수 있습니다.
UI 프레임 문구 갱신 역시 0.2초 간격으로 제한해 고빈도 수신의 UI 부담을 줄입니다.

## 8. 일반 USD와 BasisCurves 처리

```text
USD Scene의 경로 입력 / Browse
  → window._load_usd()
      → session.stop_worker(): 이전 프레임·자동 복구 중단
      → 모델 관련 UI 초기화
      → omni.usd context.open_stage_async(path)
      → usd_loader.default_basis_curves(stage)
          → 인스턴스 내부 포함 곡선 탐색
          → 곡선을 포함하는 인스턴스 조상만 세션에서 해제
          → 전체 순회로 곡선 purpose = default 기록
      → BasisCurves prim 수 / 해제한 인스턴스 수 표시
      → 장면에 카메라 맞춤
```

지원 확장자는 `.usd`, `.usda`, `.usdc`, `.usdz`입니다. 새 스테이지를 여는 기능이며 현재 장면에 reference를 추가하는 기능은 아닙니다.
탐색은 현재 합성된 스테이지 기준입니다. 참조된 곡선과 비활성 곡선도 포함하지만 선택하지 않은 variant나 미로드 payload의
미합성 내부까지 별도로 로드하여 조사하지는 않습니다.

`purpose`는 Session Layer에 명시적으로 기록하므로 상위 prim의 purpose 상속도 덮어씁니다.
인스턴스 proxy는 수정할 수 없으므로 곡선이 든 인스턴스는 세션에서 `instanceable=false`로 바꿉니다.
중첩 인스턴스는 여러 번 탐색하여 처리하며, 인스턴싱 해제로 장면 메모리/처리 비용이 늘 수 있습니다.
원본 파일은 자동 저장하지 않으며 `purpose` 변경이 visibility나 머티리얼까지 변경하지는 않습니다.
표시 개수는 **BasisCurves prim 수**이고 곡선 가닥 수나 points 수가 아닙니다.

## 9. 통신 규약과 수정 규칙

접속 주소는 같은 PC의 로컬 TCP입니다. hello 메시지의 프로토콜 버전을 확인한 뒤 통신합니다.
실제 prefix 형식은 다음과 같습니다.

```text
[u32 header_len][u32 payload_len][UTF-8 JSON header][binary payload]
```

두 길이는 little-endian이며, 배열 메타데이터에는 `name`, `dtype`, `shape`, `offset`, `nbytes`가 들어갑니다.
NumPy를 직접 import하지 않는 공용 모듈이며, 수신자는 `frombuffer`로 payload를 공유하는 배열을 만듭니다.

**원본은 저장소 루트의 `twin_worker/twin_worker/protocol.py`입니다.**
규약이나 주석을 수정할 때 원본을 수정한 뒤 루트에서 `scripts/sync_protocol.ps1`로 뷰어 사본에 반영합니다.
새 명령은 워커의 dispatch/runner 처리도 함께 구현해야 합니다.

## 10. 자동 복구와 로그

예상치 못한 연결 종료이고 이전 모델 경로가 있으면 세션이 워커 복구를 시도합니다.
최근 frame의 입력값, 모델/메쉬 경로, 실행 중이었다면 step/realtime/speed 설정을 다시 보냅니다.
실패 시 대기는 1초 → 2초 → 4초로 늘며 최대 3회 시도합니다.

현재 복구는 명령 재전송이 끝나면 성공 처리합니다. 비동기 모델 로드 완료까지 기다려 판정하지 않으며,
이전 시뮬레이션 시간/내부 상태나 아직 frame에 반영되지 않은 입력을 완전히 복원하지도 않습니다.
사용자의 Stop Worker와 USD 로드는 의도적인 종료로 처리하여 재시작하지 않습니다.

워커 stdout/stderr는 별도 읽기 스레드가 수집합니다. 최근 200줄을 보관하고 UI는 그중 최근 60줄을 표시합니다.
`debug_worker=false`에서는 Kit 디버거의 자식 Python 주입을 우회해 실행 속도 저하를 줄입니다.

## 11. 설정

`config/extension.toml`의 `/exts/twinviz.viewer` 설정입니다.

| 키 | 기본값 | 내용 |
|---|---|---|
| `worker_root` | 빈 값 | 저장소 워커 자동 탐색 |
| `worker_python` | 빈 값 | 워커 `.venv/Scripts/python.exe` |
| `models_dir` | 빈 값 | 저장소 `models/` |
| `model_units_in_meters` | `1.0` | 모델 좌표 1단위에 해당하는 미터 |
| `write_backend` | `fabric` | `fabric` 또는 `usd` |
| `debug_worker` | `false` | 워커 디버깅 허용 |
| `max_display_points` | `1000000` | TBROM별 점 상한, 0이면 무제한 |

## 12. 실행과 검증

저장소 루트에서 워커 환경을 준비합니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_worker_venv.ps1
```

`kit-app/`에서 앱을 빌드하고 실행합니다.

```powershell
.\repo.bat build
.\repo.bat launch -n twinviz.app.kit
.\repo.bat test
```

| 테스트 | 검증 범위 | 필요 환경 |
|---|---|---|
| `test_worker.py` | 실행·hello·ping·종료 | Kit + 워커 venv |
| `test_viewer.py` | 컬러맵·필드·메쉬·복구·후처리·USD backend | Kit, 실모델 테스트는 해당 `.twin`/메쉬 |
| `test_usd_loader.py` | 목적값 상속·비활성 곡선·인스턴스·원본 레이어 보존·곡선 없는 장면 | Python + OpenUSD `pxr` |

USD 단위 테스트는 Kit 없이도 `test_usd_loader.py` 파일을 직접 실행할 수 있습니다.
대형 모델의 프레임/로드 성능 측정 도구는 저장소 루트의 `scripts/kit_bench.py`, `kit_load_bench.py`,
`kit_worker_timing.py`, `kit_stream_bench.py`를 참고하세요.

## 13. 기능 수정 시 찾아갈 곳

- 버튼·레이아웃·표시 문구: `window.py`
- 실행 상태·입력 전송·복구: `session.py`
- 통신 지연·프레임 적체: `client.py`, 워커 서버
- 컬러맵·변형·Fabric 기록: `colormap.py`, `stage_updater.py`
- 곡선/등치면 표시 속성: `overlays.py`; 계산 알고리즘은 워커 `post.py`
- 일반 USD 탐색·purpose 처리: `usd_loader.py`, `window._load_usd()`
- 프로세스 실행·포트 감지·로그: `process_manager.py`

코드를 읽을 때는 `extension.py → window.py → session.py → client.py → stage_updater.py` 순서로 시작하고,
일반 USD 경로는 `window._load_usd() → usd_loader.py`를 별도로 따라가면 됩니다.
