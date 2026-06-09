Set ws = CreateObject("WScript.Shell")

' Agent API (8900)
ws.Environment("Process").Item("ENABLE_TOOLS") = "true"
ws.Run "G:\AI-DASHENG\.venv\Scripts\pythonw.exe G:\AI-DASHENG\src\agent_api.py", 0, False
WScript.Sleep 3000

' WebServer (7860)
ws.Run "G:\AI-DASHENG\.venv\Scripts\pythonw.exe G:\AI-DASHENG\src\web_server.py", 0, False

' Gateway (9090)
ws.Environment("Process").Item("GATEWAY_PORT") = "9090"
ws.Environment("Process").Item("QQ_APP_ID") = "1903881299"
ws.Environment("Process").Item("QQ_APP_SECRET") = "VTSRRRSTVYbfjotz6DLTcmw7IUgt7Lap"
ws.Run "G:\AI-DASHENG\.venv\Scripts\pythonw.exe G:\AI-DASHENG\src\gateway\server.py", 0, False
