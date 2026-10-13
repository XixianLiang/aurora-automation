"""Install a supplied Aurora or Windy APK/split archive on a selected emulator."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

PACKAGE = 'com.jrustonapps.myauroraforecast'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--package', choices=[PACKAGE, 'com.windyty.android'], default=PACKAGE)
    parser.add_argument('--source', type=Path, default=Path('com.jrustonapps.myauroraforecast_26.7.4.xapk'))
    parser.add_argument('--fresh', action='store_true', help='Remove existing Aurora and its data on the emulator before installing')
    parser.add_argument('--report', type=Path, default=Path('artifacts/aurora-install.json'))
    args = parser.parse_args()
    if not args.serial.startswith('emulator-'):
        parser.error('This local test installer accepts emulator serials only')
    sdk = Path(os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT') or Path.home() / 'Library/Android/sdk')
    adb = shutil.which('adb') or str(sdk / 'platform-tools/adb')
    aapt = shutil.which('aapt')
    if not aapt:
        candidates = sorted(sdk.glob('build-tools/*/aapt'), reverse=True)
        if not candidates:
            parser.error('aapt is required to verify APK package identity; install Android SDK build-tools')
        aapt = str(candidates[0])

    def run(*command):
        return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()

    report = {'serial': args.serial, 'source': str(args.source.resolve()),
              'requested_installer': 'com.android.vending', 'fresh': args.fresh, 'package': args.package}
    code = 1
    try:
        with tempfile.TemporaryDirectory(prefix='aurora-apks-') as directory:
            temp = Path(directory)
            if args.source.is_dir():
                apks = sorted(args.source.glob('*.apk'))
            elif args.source.suffix.lower() == '.apk':
                apks = [args.source]
            else:
                with zipfile.ZipFile(args.source) as archive:
                    names = [name for name in archive.namelist() if name.endswith('.apk')]
                    if len({Path(name).name for name in names}) != len(names):
                        raise RuntimeError('Archive contains duplicate APK filenames')
                    for name in names:
                        (temp / Path(name).name).write_bytes(archive.read(name))
                apks = sorted(temp.glob('*.apk'))
            if not apks:
                raise RuntimeError('No APK files found')
            report['files'] = []
            for apk in apks:
                badging = run(aapt, 'dump', 'badging', str(apk))
                match = re.search(r"^package: name='([^']+)'", badging)
                if not match or match[1] != args.package:
                    raise RuntimeError(f'{apk.name} has the wrong package (expected {args.package})')
                report['files'].append({'name': apk.name, 'sha256': hashlib.sha256(apk.read_bytes()).hexdigest()})
            # Verify files before --fresh can remove emulator app data.
            installed = run(adb, '-s', args.serial, 'shell', 'pm', 'list', 'packages', args.package)
            if args.fresh and f'package:{args.package}' in installed.splitlines():
                print(run(adb, '-s', args.serial, 'uninstall', args.package), flush=True)
            command = [adb, '-s', args.serial, 'install-multiple', '-i', 'com.android.vending']
            if not args.fresh:
                command.append('-r')
            print(run(*command, *(str(apk) for apk in apks)), flush=True)
            installer = run(adb, '-s', args.serial, 'shell', 'pm', 'list', 'packages', '-i', args.package)
            if 'installer=com.android.vending' not in installer:
                raise RuntimeError(f'Unexpected installer metadata: {installer}')
            report.update(status='success', installer_metadata=installer)
            code = 0
    except (OSError, RuntimeError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        detail = (error.stderr or error.stdout) if isinstance(error, subprocess.CalledProcessError) else str(error)
        report.update(status='error', error=detail.strip())
        print(report['error'])
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Install report: {args.report.resolve()}')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
