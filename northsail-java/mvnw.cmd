@REM Maven Wrapper - run "mvn -N wrapper:wrapper" first if mvnw fails
@echo off
setlocal
set "MAVEN_PROJECTBASEDIR=%~dp0"
cd /d "%MAVEN_PROJECTBASEDIR%"

where mvn >nul 2>&1
if %ERRORLEVEL% equ 0 (
    mvn %*
) else (
    echo Maven not found. Install Maven or run from IDE: File -^> Open -^> northsail-java (select pom.xml)
    echo Then: Maven -^> Reload Project
    exit /b 1
)
