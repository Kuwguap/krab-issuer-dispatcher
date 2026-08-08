"""Script to find and stop any running bot processes."""
import subprocess
import sys
import os

def find_bot_processes():
    """Find Python processes running bot.py."""
    try:
        # Get all Python processes
        result = subprocess.run(
            ['powershell', '-Command', 
             'Get-Process python -ErrorAction SilentlyContinue | '
             'Where-Object {$_.Path -like "*python*"} | '
             'Select-Object Id, ProcessName, Path | Format-Table -AutoSize'],
            capture_output=True,
            text=True
        )
        
        if result.stdout:
            print("Python processes found:")
            print(result.stdout)
        else:
            print("No Python processes found.")
            
        # Try to find processes with bot.py in command line
        result2 = subprocess.run(
            ['powershell', '-Command',
             'Get-WmiObject Win32_Process | '
             'Where-Object {$_.CommandLine -like "*bot.py*"} | '
             'Select-Object ProcessId, CommandLine | Format-List'],
            capture_output=True,
            text=True
        )
        
        if result2.stdout and 'ProcessId' in result2.stdout:
            print("\nBot processes found:")
            print(result2.stdout)
            
            # Ask if user wants to kill them
            print("\nTo stop these processes, use:")
            print("  taskkill /F /PID <ProcessId>")
            print("\nOr run this script with --kill flag (be careful!)")
        else:
            print("\nNo bot.py processes found in command line.")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--kill":
        print("WARNING: This will kill ALL Python processes!")
        response = input("Are you sure? (yes/no): ")
        if response.lower() == "yes":
            try:
                subprocess.run(['taskkill', '/F', '/IM', 'python.exe'], check=False)
                print("Python processes killed.")
            except Exception as e:
                print(f"Error: {e}")
        else:
            print("Cancelled.")
    else:
        find_bot_processes()


