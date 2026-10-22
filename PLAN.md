# PyTwin TBROM + Omniverse Kit 가시화 확장 — 계획

## 목표
Ansys PyTwin ROM(TBROM 필드 포함)을 **별도 프로세스**에서 실행하고, 시뮬레이션 결과를
Omniverse Kit 앱(Kit **110.1.4**)에 실시간으로 렌더링한다. 워커와 Kit은 **같은 PC**에서 동작한다.

## 아키텍처

```
┌──────────────── Kit App (Kit 내장 Python) ─────────────────────┐
│  twinviz.viewer 확장 (앱: twinviz.app)                                                │
│   ├─ ProcessManager : 워커 실행/종료/헬스체크 (subprocess)       │
│   ├─ TwinClient     : asyncio TCP 클라이언트 (제어 송신, 프레임 수신) │
│   ├─ FrameBuffer    : 최신 프레임만 유지 (latest-wins)           │
│   ├─ StageUpdater   : update 이벤트에서 USD/Fabric 반영, 컬러맵    │
│   └─ UI (omni.ui)   : 시작/정지, 입력 슬라이더, 컬러맵 범위, 출력   │
└───────────────▲──────────────────────────┬──────────────────────┘
                │ 프레임(JSON 헤더 + binary)  │ 제어(JSON)
                │ 127.0.0.1 TCP             ▼
┌──────────────── Twin Worker (별도 venv, pytwin) ────────────────┐
│   ├─ Server        : asyncio TCP 서버                           │
│   ├─ TwinRunner    : TwinModel 로드 → initialize → step 루프(스레드) │
│   └─ FieldExtractor: TBROM points / snapshot → float32 numpy     │
└─────────────────────────────────────────────────────────────────┘
```

### 프로세스 분리 이유
- Kit 내장 Python/패키지와 pytwin 의존성(numpy, pandas, Twin Runtime DLL) 충돌 회피
- Twin Runtime의 블로킹 호출·네이티브 크래시로부터 Kit UI 보호

## IPC 프로토콜
- 전송: 127.0.0.1 TCP, asyncio 스트림 (추가 의존성 없음)
- 프레이밍: `[u32 헤더길이][JSON 헤더][u32 payload길이][binary payload]` (little-endian)
- `protocol.py`는 표준 라이브러리만 사용 → Kit/워커 공용
- 메시지
  - Kit → 워커: `ping`, `load`, `init`, `start`, `pause`, `set_inputs`, `stop`, `shutdown`
  - 워커 → Kit: `pong`, `model_info`(입출력/TBROM 목록), `geometry`(TBROM 점 N×3, 연결 시 1회),
    `mesh_map`(메쉬 정점→TBROM 점 인덱스, 1회), `frame`(t, step, scalars, 필드 값), `error`, `log`
- 매 프레임은 필드 **값만** 전송 (스칼라장 N, 벡터장 N×3 float32)
- 역압: 워커는 최신 프레임만 송신, Kit은 렌더 프레임당 최신 1개만 반영
- 대용량(수백만 점)일 때만 `multiprocessing.shared_memory` 링버퍼로 전환 검토

## 시각화
- 컬러맵은 Kit 측 numpy에서 계산 (UI에서 범위/맵 변경 시 워커 재시작 불필요)
- 메쉬가 **없는** TBROM: `UsdGeom.Points` 포인트 클라우드 + `primvars:displayColor`
- 메쉬가 **있는** TBROM: 원본 메쉬(.vtk/.stl/.cdb 등) → USD 변환, 워커에서 KD-tree로
  정점↔TBROM 점 최근접 인덱스 1회 계산 → Kit은 `values[index]`로 정점 색만 갱신
- USD 갱신: `Sdf.ChangeBlock` + `Vt.*Array.FromNumpy`로 시작, 필요시 USDRT/Fabric

## 마일스톤 (각 단계 완료 시 GitHub push)

| 단계 | 내용 | 완료 기준 |
|---|---|---|
| **M0** | 저장소/Kit 앱 골격(kit-app-template 110), 워커 venv, 공용 프로토콜, ping/pong | 확장에서 워커 실행 → ping/pong 성공, 종료 시 워커 정리 |
| **M1** | 워커: TBROM twin 로드 → step 루프 → `model_info`/`geometry`/`frame` 송신 | 테스트 클라이언트로 프레임 수신 확인 |
| **M2** | Kit: 포인트 클라우드 + 컬러맵, 입력 슬라이더, 스칼라 출력 표시 | 입력 변경 시 필드 색이 실시간 변화 |
| **M3** | 메쉬 매핑(메쉬 있는 모델): 변환 스크립트 + 인덱스 매핑 + 서피스 컨투어 | 메쉬 표면 컨투어 표시 |
| **M4** | 성능/운영: USDRT/Fabric, 워커 자동 재시작, named selection 선택, 로그 패널 | 10만 점 이상 30fps |

## 디렉터리 구조 (예정)
```
works/
├─ PLAN.md
├─ kit-app/                         # kit-app-template 기반
│  └─ source/extensions/twinviz.viewer/
│     ├─ config/extension.toml      # worker_root, worker_python 설정
│     └─ twinviz/viewer/
│        ├─ extension.py
│        ├─ process_manager.py
│        ├─ client.py
│        ├─ protocol.py             # twin_worker/protocol.py와 동일
│        ├─ stage_updater.py
│        └─ ui/window.py
├─ twin_worker/
│  ├─ pyproject.toml                # pytwin, numpy, scipy
│  └─ twin_worker/
│     ├─ __main__.py                # --port --twin --parent-pid
│     ├─ server.py
│     ├─ runner.py
│     └─ protocol.py
├─ models/                          # .twin, 메쉬 (git 제외 또는 LFS)
└─ scripts/
```

## 운영 규칙
- 워커는 `--parent-pid`를 감시해 Kit 종료 시 자동 종료 (고아 프로세스 방지)
- USD 수정은 Kit 메인 스레드(update 이벤트)에서만
- 각 마일스톤 완료 시 커밋 후 GitHub push

## 리스크 / 확인 사항
- PyTwin 지원 Python 버전 → 워커 venv용 Python 버전 결정
- Twin Runtime 실행 라이선스 조건
- TBROM API(필드 출력 활성화, snapshot 반환 형태)는 설치 버전 기준으로 확인
- kit-app-template 110 브랜치 구조 및 Kit 내장 Python 버전 확인

## 진행 기록
- 예제 모델 조사 (`scripts/inspect_twin.py`, `pytwin[graphics]` 필요 — TBROM points 읽기에 pyvista 사용)
  | 모델 | TBROM / 필드 | 점 수 | 위치 | 스냅샷 방식 | 비용 |
  |---|---|---:|---|---|---|
  | `FEADeformationTBROM_23R2` | DeformationROM1 / Deformation (벡터 N×3), 입력 `Force_X_Component` | 1,332 | Nodal | 메모리 `generate_snapshot` (모드 계수가 twin 출력) | step 1 ms, snapshot 0.03 ms |
  | `ThermalTBROM_23R2` | ThermalROM23R1_1 / Static_Temperature (스칼라), 입력 main/side inlet temp | 139,868 | elemental | 디스크: `*_store_snapshots=1` + `initialize_evaluation` 시 `snapshot_0.bin` (정적 ROM, step으로 갱신 안 됨) | 입력 변경당 재초기화 ~550 ms |
  - 결론: 워커는 **두 가지 스냅샷 모드**를 지원 — `memory`(step마다 generate_snapshot) / `static`(입력 변경 시 재초기화 후 디스크 스냅샷 읽기)
  - ThermalTBROM은 elemental 데이터 → points는 요소 중심점
- M3: 메쉬 매핑. 워커 `mesh.py` — `<twin>.twinviz.json` 사이드카(또는 `load{meshes}`)로 메쉬 지정,
  `.rst`/`.cdb`/`.dat`는 `ansys-mapdl-reader`(Ansys 설치 불필요), 그 외는 pyvista. 외곽 표면 삼각화 →
  cKDTree로 각 정점의 최근접 TBROM 점 인덱스(허용거리 = TBROM 점 간격 중앙값×1.5, 밖이면 -1=회색).
  `mesh_map` 메시지(ROM별 points/faces/index)를 로드 시 1회 전송, 재접속 시 재전송. Kit은 `UsdGeom.Mesh`에
  `values[index]`로 정점 색/변형만 갱신, Show: mesh/points 전환(보이는 쪽만 갱신).
  FEA 예제: 표면 정점 1,332 / 삼각형 2,352, 미매핑 0 (노드 좌표 일치 ~1e-11 m).
  - elemental 검증(Thermal + `ThermalTBROM.dat`): `ansys-mapdl-reader` Archive가 첫 EBLOCK만 읽음(231개 중 1개)
    → 자체 `.cdb/.dat` 파서(`cdb.py`: 모든 NBLOCK/EBLOCK, 솔리드 요소만, 퇴화 hex→tet/wedge/pyramid).
    솔리드 14,454요소 → 표면 4,548정점/9,088삼각형. 허용거리 배수: nodal 1.5 / elemental 4.0(사이드카 `tolerance_factor`로 조정).
    본체 100% 매핑, 볼트 등 ROM 범위 밖 부품은 회색(미매핑 490정점, 10.8%).
- 렌더링 보정: 컬러맵을 sRGB→linear로 변환해 `displayColor`에 기록(이전엔 sRGB 값을 linear로 해석해 색이 옅었음).
  `UsdPreviewSurface` 머티리얼(diffuse=`displayColor`, emissive=`fieldEmission`=색×0.6)로 조명과 무관하게 값 판독 가능.
  MAPDL 표면 면 방향이 전부 안쪽이었음 → `compute_normals(auto_orient_normals)`로 바깥쪽 정렬.
  `kit_screenshot.py`는 뷰포트를 창 크기와 무관하게 1280×720으로 별도 캡처(`*_viewport.png`).
- M2: Kit 측 `session.py`(워커+클라이언트+스테이지 연결, 입력 스로틀: static은 프레임 도착까지 대기, dynamic은 50 ms),
  `stage_updater.py`(`GLOBAL_EVENT_UPDATE`에서 최신 프레임만 반영, ROM별 `UsdGeom.Points` + `primvars:displayColor`,
  벡터장은 크기로 색칠 + 변형 배율), `colormap.py`(turbo/viridis/coolwarm/jet), `window.py`(Property 패널 옆 도킹:
  워커/모델 로드/실행/입력/출력/표시 설정). 단위는 SI→스테이지 metersPerUnit 변환.
  화면 확인: `scripts/kit_screenshot.py` (`--exec`로 실행, 결과 `_screenshots/`, git 제외).
  - 남은 개선(M4): 일시정지 중 dynamic 입력 변경 시 미리보기 없음
- M1: `twin_worker/runner.py` — TwinModel 전용 스레드, 명령 큐. 로드 시 모드 자동 판별(dynamic/static).
  `load` → `model_info` + `geometry`(ROM별 float32 N×3) + 초기 `frame`. dynamic: `start{step_size, realtime, speed}`/`pause`/`stop`(재초기화),
  `set_inputs`는 다음 스텝에 반영. static: `set_inputs`마다 재초기화 후 프레임 1개. 프레임 필드는 float32, 헤더에 t/step/inputs/scalars.
  서버는 신뢰 메시지 큐 + 최신 프레임 1개(latest-wins), 재접속 시 model_info/geometry 재전송. 신규 메시지 `state{running, mode}`.
  테스트: `twin_worker/tests/test_runner.py` (models/ 파일 없으면 skip)
- M0: kit-app-template 110.1.4에서 앱 `twinviz.app`(kit_base_editor), 확장 `twinviz.viewer`(python_ui) 생성. 워커 venv Python 3.12 + pytwin 0.13.0. 포트는 워커가 0번(자동)으로 열고 stdout `TWIN_WORKER_READY port=N`으로 Kit에 전달.
