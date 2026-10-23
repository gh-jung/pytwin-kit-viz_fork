# 폐쇄망 실모델 점검

결과를 손으로 옮겨 적어야 하는 환경을 위한 절차입니다. 점검 스크립트가 모델당 한 줄의 짧은 코드만 출력하고,
모델 파일명은 출력하지 않습니다(번호 M1, M2…는 `models/`의 `.twin` 파일명 정렬 순서).

## 사람이 미리 할 일
1. `models/`에 `.twin` 파일을 넣습니다.
2. 메쉬가 있는 모델은 twin 옆에 `<twin 이름>.twinviz.json`을 만듭니다 (`{"mesh": "메쉬파일.rst"}`, 형식은 [models/README.md](../models/README.md)).
3. 워커 venv(`twin_worker\.venv`)와 Kit 빌드(`kit-app\_build`)가 준비되어 있어야 합니다.

## 로컬 LLM 프롬프트
아래 블록을 그대로 붙여넣고 `<REPO>`만 실제 경로로 바꿉니다.

```text
너는 폐쇄망 PC에서 TwinViz 점검 명령을 실행하는 보조자다.
규칙: 아래 명령만 순서대로 실행한다. 최종 답에는 지정한 출력 줄만 원문 그대로, 한 줄에 하나씩 쓴다.
설명, 요약, 해석, 인사, 마크다운, 모델 파일명, 경로를 절대 쓰지 않는다. 숫자를 바꾸거나 반올림하지 않는다.

작업 폴더: <REPO>

[1단계] 실행:
  twin_worker\.venv\Scripts\python scripts\field_check.py
  -> 출력된 모든 줄(ENV 줄, M1~Mn 줄)을 그대로 옮긴다.

[2단계] models 폴더의 *.twin 파일을 파일명 오름차순으로 정렬하고, 각 파일 F마다 실행:
  kit-app\_build\windows-x86_64\release\twinviz.app.kit.bat --exec scripts\kit_screenshot.py --/twinviz/shot/model=F --/twinviz/shot/run=5 --/twinviz/shot/realtime=0 --/twinviz/shot/settle=15 --/twinviz/shot/report=1
  -> 출력에서 K 뒤에 숫자로 시작하는 줄 하나만 옮긴다 (예: K1 ok col=ok ...).
  -> 한 파일에 5분이 넘으면 중단하고 K<번호> ERR TIMEOUT 을 쓴다.

[오류] 명령 자체가 실행되지 않으면 X<단계번호> 와 오류 메시지 첫 줄의 앞 40자를 쓴다.

최종 답 예시:
ENV py=3.12.10 pytwin=0.13.0 pv=0.49.1 mr=0.56.0
M1 ok m=d r=1 p=1332 d=- f=v l=0.5 t=1.0 rt=0.8 fr=0.2 sn=0.0 nm=10 nan=0 mesh=rst/2352/0.0
K1 ok col=ok disp=mesh fps=117 rs=0 we=0 lh=120 rh=9/14
```

화면이 실제로 맞게 보이는지는 사람이 `_screenshots\<모델>_viewport.png`를 보고 판단해서,
필요하면 `V1 ok` / `V1 bad <한두 단어>`처럼 한 줄을 덧붙여 주세요.

## 출력 해석

### `field_check.py` (M 줄)
```
ENV  py, pytwin, pv (pyvista), mr (ansys-mapdl-reader) 버전; '-' = 미설치
Mn   모델 번호 (파일명 대소문자 무시 정렬; K 번호와 동일); ok | ERR <단계> <오류>    (단계: LOAD INIT STEP MESH)
m    d = dynamic(스텝 진행), s = static(입력 변경마다 재초기화)
r    TBROM 개수
p    TBROM별 전체 점 개수 ('+'로 연결)
d    점 상한(--max-points, 기본 100만) 초과 시 실제 계산·전송·표시하는 점 수:
     <n>s = pytwin을 표본으로 축소(빠름), <n>c = 전체 계산 후 잘라냄, '-' = 전체 사용
f    TBROM별 필드: s = 스칼라, v = 벡터, x<k> = k성분
l    로드 시간 [s] (생성 + 초기화 + 좌표 + 메쉬 매핑)
t    d: 스텝당 ms (스냅샷 포함), s: 입력 변경당 ms (재초기화 + 읽기)
rt   d: t 중 pytwin 스텝 시간 (twin 런타임 계산 + pytwin 출력 처리, 필드 재구성 제외)
fr   d: t 중 TBROM 필드 재구성 시간 (모드 x 기저)
sn   d: t 중 스냅샷 생성 + 프레임 패킹 시간
nm   TBROM별 모드 수
ss   1 = 런타임이 스냅샷 파일을 씀 (static 모델만 1, dynamic은 0이어야 정상)
nan  마지막 프레임의 NaN/inf 개수
mesh 확장자/삼각형 수/미매핑 % ('-' = 사이드카 없음, ERR(<원인>) = 실패)
```

### `kit_screenshot.py` (K 줄)
```
Kn   모델 번호 (M과 같은 순서); ok | ERR <오류>
col  ok = 색이 모델 위에서 변함, flat = 한 색, none = 반영된 프레임 없음
disp mesh | points (표시된 형태)
fps  실행 중 스테이지에 반영된 초당 프레임 (dynamic만, 그 외 '-')
rs   워커 자동 재시작 횟수
we   워커 오류 로그 줄 수
lh   로드: Load부터 첫 프레임 표시까지 가장 긴 앱 업데이트 [ms] (멈춤)
rh   실행: 앱 업데이트 p95/최대 [ms] (멈춤; 8~17이면 정상, 50 이상이면 버벅임)
```
