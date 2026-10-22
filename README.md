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

## 테스트
```powershell
# 워커 단독
cd twin_worker; .\.venv\Scripts\python -m unittest discover -s tests -v

# Kit 확장 (워커 실행 → ping → 종료)
cd kit-app; .\repo.bat test
```

## 프로토콜 변경 시
`twin_worker/twin_worker/protocol.py`가 원본입니다. 수정 후 `scripts\sync_protocol.ps1`로 확장에 복사하세요.
