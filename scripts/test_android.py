"""Run real model smoke tests on an explicitly selected Android test device.

Build app + assembleDebugAndroidTest for the device ABI first. The runner imports
the supplied packages into the app and therefore replaces its selected models.
Non-empty output checks establish inference capability, not translation quality.
"""
import argparse, json, subprocess, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'com.vt.voicetranslator'

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--adb', required=True, type=Path)
    p.add_argument('--serial', required=True)
    p.add_argument('--app-apk', type=Path, default=ROOT/'app/build/outputs/apk/debug/app-debug.apk')
    p.add_argument('--timeout', type=int, default=600)
    a = p.parse_args()
    base = [str(a.adb), '-s', a.serial]
    def adb(*args, **kwargs):
        return subprocess.run(base+list(args), check=True, **kwargs)
    adb('install', '-r', str(a.app_apk))
    adb('install', '-r', str(ROOT/'app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk'))
    remote = f'/sdcard/Android/data/{PACKAGE}/files'
    adb('shell', 'mkdir', '-p', remote)
    for name in ('sensevoice-zh-ja-ko-en.vtmodel', 'qwen25-3b-zh-ja-ko-en.vtmodel'):
        adb('push', str(ROOT/'dist/models'/name), remote+'/'+name)
    wavs = ROOT/'model-work/upstream/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/test_wavs'
    for code in ('zh', 'ja', 'ko', 'en'):
        adb('push', str(wavs/(code+'.wav')), remote+'/'+code+'.wav')
    # Remove only the previous test report to distinguish fresh results.
    adb('shell', 'rm', '-f', remote+'/android-smoke.json')
    out = ROOT/'dist/verification'; out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    timeout = False
    try:
        adb('shell', 'am', 'instrument', '-w',
            PACKAGE+'.test/'+PACKAGE+'.SmokeInstrumentation', timeout=a.timeout)
    except subprocess.TimeoutExpired:
        timeout = True
        adb('shell', 'am', 'force-stop', PACKAGE)
    result = subprocess.run(base+['pull', remote+'/android-smoke.json', str(out/'android-smoke.json')])
    metadata = {'serial': a.serial, 'elapsedSeconds': round(time.time()-started, 1),
                'timedOut': timeout, 'reportRetrieved': result.returncode == 0,
                'scope': 'Package import and non-empty ASR/translation smoke tests; not a quality benchmark'}
    (out/'android-test-run.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    if timeout or result.returncode:
        raise SystemExit('Android test did not complete; see partial report, if available.')
    report = json.loads((out/'android-smoke.json').read_text(encoding='utf-8'))
    if report.get('status') != 'passed':
        raise SystemExit('Android smoke test failed: '+str(report.get('error')))
    print('Android model smoke tests passed. Review translations manually.')

if __name__ == '__main__':
    main()
