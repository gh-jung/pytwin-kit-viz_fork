# models

`.twin` 파일과 원본 메쉬를 이 폴더에 둡니다. 용량/라이선스 문제로 git에는 포함하지 않습니다.

## 메쉬 지정 (선택)
TBROM 필드를 점 대신 메쉬 표면에 그리려면 twin 옆에 `<twin 이름>.twinviz.json`을 둡니다.

```json
{ "mesh": "FEA_deformation.rst" }
```
또는 TBROM별로:
```json
{ "meshes": { "DeformationROM1": "FEA_deformation.rst" } }
```
경로는 json 파일 기준 상대 경로. 지원 형식: MAPDL `.rst`, `.cdb`/`.dat`, 그리고 pyvista가 읽는 형식(`.vtk`, `.vtu`, `.stl`, `.obj`, `.ply` 등).
메쉬 좌표 단위는 TBROM 점과 같아야 합니다 (최근접 점으로 매핑).
