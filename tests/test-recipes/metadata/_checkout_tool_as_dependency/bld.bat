svn upgrade
svn info
if errorlevel 1 exit 1
for /f "delims=" %%i in ('svn info ^| grep "Revision"') do set svnrev=%%i
if errorlevel 1 exit 1
echo %svnrev%
if not "%svnrev%"=="Revision: 1" exit 1