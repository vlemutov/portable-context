"""Run the actual Linux identity bootstrap during image build, before deployment."""
import shutil
import server

try:
    server.configure('https://synthetic.example.invalid')
    assert server.STATE['ready']
    print('Synthetic Pocket ID bootstrap passed')
finally:
    process = server.STATE.get('process')
    if process is not None and process.poll() is None:
        process.terminate()
        process.wait(timeout=10)
    directory = server.STATE.get('directory')
    if directory:
        shutil.rmtree(directory)
