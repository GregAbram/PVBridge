# Headless test of pvbridge's command handling (no GUI timer): pvpython test_bridge.py
import os, sys, shutil, subprocess, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pvbridge
root = tempfile.mkdtemp(prefix="pvbridge-test-")
pvbridge.ROOT = root
pvbridge._setup_dirs(); pvbridge._new_namespace()
os.environ["HOME_PVB"] = root
send = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pvsend")

def client(code, results, key):
    env = dict(os.environ, HOME=os.path.dirname(root))
    # pvsend uses ~/.pvbridge; point it at the test root via a symlinked HOME
    p = subprocess.run([sys.executable if False else "python3", send, "-t", "20", code],
                       capture_output=True, text=True, env=env)
    results[key] = (p.returncode, p.stdout, p.stderr)

# pvsend expects $HOME/.pvbridge: make a fake HOME containing that name
home = tempfile.mkdtemp(prefix="pvb-home-")
os.symlink(root, os.path.join(home, ".pvbridge"))
def client(code, results, key):
    p = subprocess.run(["python3", send, "-t", "20", code], capture_output=True, text=True,
                       env=dict(os.environ, HOME=home))
    results[key] = (p.returncode, p.stdout, p.stderr)

tests = {
    "expr": "1+2",
    "print": "s = Sphere(ThetaResolution=16)\nprint('made', s.GetXMLLabel())",
    "persist": "len(GetSources()), s.ThetaResolution",
    "modify": "s.ThetaResolution = 32\ns.UpdatePipeline()\ns.GetDataInformation().GetNumberOfPoints()",
    "error": "1/0",
}
results = {}
for k, code in tests.items():
    t = threading.Thread(target=client, args=(code, results, k)); t.start()
    for _ in range(100):
        time.sleep(0.05); pvbridge.poll()
        if not t.is_alive():
            break
    t.join()
    rc, out, err = results[k]
    print("%-8s rc=%d out=%r err=%r" % (k, rc, out.strip()[-120:], err.strip()[-80:]))

# a group-writable command file must be rejected, not run
bad = os.path.join(root, "in", "zz-bad.py")
open(bad, "w").write("print('should not run')"); os.chmod(bad, 0o664)
pvbridge.poll()
print("rejected:", os.listdir(os.path.join(root, "rejected")), "out:", "zz-bad.txt" in os.listdir(os.path.join(root, "out")))
print("ROOT mode", oct(os.stat(root).st_mode & 0o777))
shutil.rmtree(root); shutil.rmtree(home)
