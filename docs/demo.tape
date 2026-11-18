# Genera docs/demo.gif (necesita vhs):
#   docs/demo-setup.sh && PATH="/tmp/logguard-demo/bin:$PATH" vhs docs/demo.tape
Output docs/demo.gif
Set Shell "bash"
Set FontSize 15
Set Width 1300
Set Height 360
Set Padding 12
Set TypingSpeed 60ms

Hide
Type "export PATH=/tmp/logguard-demo/bin:$PATH && PS1='$ ' && cd /tmp && clear" Enter
Sleep 1s
Show

Type "logguard --status" Sleep 500ms Enter
Sleep 2s
Type "logguard -v" Sleep 500ms Enter
Sleep 3s
Type "logguard --status" Sleep 500ms Enter
Sleep 2s
Type "ls /Volumes/LOGDEMO/log" Sleep 500ms Enter
Sleep 3s
