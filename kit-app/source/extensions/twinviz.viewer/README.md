# TwinViz Viewer

PyTwin 워커의 TBROM 결과를 Omniverse Kit에서 표시하고, 일반 USD 장면을 로드하는 확장입니다.

**[전체 구조와 실행 흐름 읽기](docs/README.md)**

- `.twin`: UI → 세션 → 워커 계산 → TCP 수신 → NumPy 준비 → USD/Fabric 반영
- USD: 워커 정지 → 스테이지 열기 → BasisCurves 탐색 → 세션 레이어에 `purpose = default` 설정

상세 문서에는 모듈별 역할, 스레드 경계, 메시지 규약, 입력 제한, 자동 복구, 설정과 테스트 방법이 포함되어 있습니다.
`config/extension.toml`의 Extension Manager 문서도 동일한 `docs/README.md`를 사용합니다.
