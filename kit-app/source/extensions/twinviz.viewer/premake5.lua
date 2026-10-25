-- Use folder name to build extension name and tag. Version is specified explicitly.
-- 확장 폴더 이름에서 빌드 대상 정보를 얻는다. 런타임 기능은 Python 패키지에서 구현한다.
local ext = get_current_extension_info()

project_ext (ext)

-- Link only those files and folders into the extension target directory
repo_build.prebuild_link {
    -- 복사 대신 링크하여 소스/문서/이미지 변경을 빌드된 확장에서도 참조한다.
    { "data", ext.target_dir.."/data" },
    { "docs", ext.target_dir.."/docs" },
    { "twinviz", ext.target_dir.."/twinviz" },
}
