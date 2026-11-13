import json
import sqlite3
import subprocess
import sys
import types
from urllib.parse import parse_qs, urlsplit

import pytest

import robust
import ups_track
from tools import local_ups_channel as channel


NUMBER = '1Z999AA10123456784'


@pytest.fixture
def local_channel(monkeypatch, tmp_path):
    monkeypatch.setattr(channel, 'OUT_FILE', tmp_path / 'results.json')
    monkeypatch.setattr(channel, 'WORK_FILE', tmp_path / 'work.json')
    monkeypatch.setattr(ups_track, 'track_ups', lambda number, **kwargs: {
        'tracking': number, 'ok': True, 'stage': '运输中', 'events': []})
    monkeypatch.setitem(sys.modules, 'patchright.sync_api', types.SimpleNamespace(
        sync_playwright=lambda: pytest.fail('local channel must use the shared tracker')))
    return {'order': 'XSD-TEST', 'tracking': NUMBER, 'carrier': 'UPS',
            'status': '运输中', 'active': True, 'binding_version': 3}


def test_daily_refresh_retries_cached_failure(local_channel):
    channel.OUT_FILE.write_text(json.dumps({NUMBER: {**local_channel, 'ok': False}}), encoding='utf-8')
    result = channel.fetch_pending([local_channel], refresh_active=True)
    assert result[NUMBER]['ok'] is True
    assert result[NUMBER]['binding_version'] == 3
    assert json.loads(channel.OUT_FILE.read_text(encoding='utf-8'))[NUMBER]['ok'] is True


@pytest.mark.parametrize('change', [{'order': 'OLD-ORDER'}, {'binding_version': 2}])
def test_cache_from_another_binding_is_requeried(local_channel, change):
    channel.OUT_FILE.write_text(json.dumps({NUMBER: {**local_channel, **change, 'ok': True}}), encoding='utf-8')
    result = channel.fetch_pending([local_channel])
    assert result[NUMBER]['order'] == 'XSD-TEST'
    assert result[NUMBER]['binding_version'] == 3
    assert 'observed_at' in result[NUMBER]


@pytest.mark.parametrize('change', [{'active': False}, {'status': '签收'}, {'status': '退回'}])
def test_inactive_and_terminal_packages_are_not_queried(local_channel, change):
    assert channel.fetch_pending([{**local_channel, **change}], retry_failed=True, refresh_active=True) == {}
    assert not channel.OUT_FILE.exists()


def test_corrupt_cache_fails_closed(local_channel):
    channel.OUT_FILE.write_text('{broken', encoding='utf-8')
    with pytest.raises(ValueError):
        channel.fetch_pending([local_channel], refresh_active=True)
    assert channel.OUT_FILE.read_text(encoding='utf-8') == '{broken'


def test_failed_atomic_replace_preserves_cache_and_aborts(local_channel, monkeypatch):
    original = json.dumps({NUMBER: {**local_channel, 'ok': False}})
    channel.OUT_FILE.write_text(original, encoding='utf-8')
    monkeypatch.setattr(robust.os, 'replace', lambda *_args: (_ for _ in ()).throw(OSError('disk error')))
    with pytest.raises(OSError):
        channel.fetch_pending([local_channel], refresh_active=True)
    assert channel.OUT_FILE.read_text(encoding='utf-8') == original


def test_all_failed_queries_are_persisted_but_fail_the_run(local_channel, monkeypatch):
    monkeypatch.setattr(ups_track, 'track_ups', lambda number, **kwargs: {
        'tracking': number, 'ok': False, 'error': 'no GetStatus data'})
    with pytest.raises(RuntimeError, match='no successful'):
        channel.fetch_pending([local_channel], refresh_active=True)
    saved = json.loads(channel.OUT_FILE.read_text(encoding='utf-8'))
    assert saved[NUMBER]['ok'] is False
    assert saved[NUMBER]['order'] == 'XSD-TEST'


def test_main_rejects_concurrent_local_run_before_export(local_channel, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['local_ups_channel.py', '--skip-apply'])
    monkeypatch.setattr(channel, 'export_worklist', lambda: pytest.fail('concurrent run must not export'))
    with robust.FileLock(channel.OUT_FILE.with_suffix('.lock'), timeout=1):
        with pytest.raises(TimeoutError):
            channel.main()


def test_first_empty_worklist_succeeds_without_missing_results_upload(local_channel, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['local_ups_channel.py'])
    monkeypatch.setattr(channel, 'export_worklist', lambda: [])
    monkeypatch.setattr(channel, 'apply_results', lambda: pytest.fail('no results file to upload'))
    channel.main()
    assert not channel.OUT_FILE.exists()


@pytest.mark.parametrize('failed_step', range(3))
def test_apply_stops_at_first_failed_docker_step(monkeypatch, failed_step):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if len(calls) - 1 == failed_step else 0, b'', b'failed')

    monkeypatch.setattr(channel.subprocess, 'run', run)
    with pytest.raises(subprocess.CalledProcessError):
        channel.apply_results()
    assert len(calls) == failed_step + 1


def test_export_failure_does_not_replace_worklist(local_channel, monkeypatch):
    channel.WORK_FILE.write_text('[]', encoding='utf-8')
    monkeypatch.setattr(channel.subprocess, 'run', lambda args, **kwargs: subprocess.CompletedProcess(args, 1, b'', b'failed'))
    with pytest.raises(subprocess.CalledProcessError):
        channel.export_worklist()
    assert channel.WORK_FILE.read_text(encoding='utf-8') == '[]'


def test_export_preserves_package_activity_and_status(monkeypatch, tmp_path, capsys):
    path = tmp_path / 'shipments.db'
    connect = sqlite3.connect
    with connect(path) as db:
        db.execute('CREATE TABLE shipments(order_no TEXT, payload TEXT)')
        db.execute('INSERT INTO shipments VALUES (?, ?)', ('XSD-TEST', json.dumps({
            'status': '部分签收', 'packages': [{'tracking': NUMBER, 'carrier': 'UPS',
                                         'active': False, 'status': '签收', 'binding_version': 3}]})))
    monkeypatch.setattr(sqlite3, 'connect', lambda *args, **kwargs: connect(path))
    exec(channel.EXPORT_CODE, {})
    exported = json.loads(capsys.readouterr().out)
    assert exported[0]['active'] is False
    assert exported[0]['status'] == '签收'


def test_apply_runs_server_merge_and_wire_in_one_locked_process(monkeypatch):
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, b'ok', b'')

    monkeypatch.setattr(channel.subprocess, 'run', run)
    channel.apply_results()
    assert len(commands) == 3
    assert commands[-1][-4:] == ['python', '/tmp/merge_local.py', '/tmp/local_ups_results.json', '--apply']
    assert 'sh' not in commands[-1]


@pytest.mark.parametrize('browser_channel', [None, 'chrome'])
@pytest.mark.parametrize('matching_response', [True, False])
def test_shared_tracker_drains_matching_response_outside_callback(monkeypatch, browser_channel, matching_response):
    clock = [0.0]
    launches = []

    class Page:
        handler = None
        in_callback = False
        waits = 0

        def on(self, event, handler):
            self.handler = handler

        def send(self, number):
            page = self

            class Response:
                url = 'https://webapis.ups.com/track/api/Track/GetStatus'

                def json(self):
                    assert not page.in_callback, 'response body must be read outside event callback'
                    return {'trackDetails': [{'trackingNumber': number,
                                             'packageStatusType': 'I', 'packageStatus': 'On the Way'}]}

            self.in_callback = True
            self.handler(Response())
            self.in_callback = False

        def goto(self, url, **kwargs):
            self.number = parse_qs(urlsplit(url).query)['tracknum'][0]
            self.send('OTHER-NUMBER')

        def query_selector(self, selector):
            return None

        def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000
            self.send(self.number if matching_response else 'OTHER-NUMBER')

    browser = types.SimpleNamespace(new_context=lambda **kwargs: types.SimpleNamespace(new_page=Page), close=lambda: None)

    def launch(**kwargs):
        launches.append(kwargs)
        return browser

    class Playwright:
        def __enter__(self):
            return types.SimpleNamespace(chromium=types.SimpleNamespace(launch=launch))

        def __exit__(self, *args):
            pass

    monkeypatch.setitem(sys.modules, 'patchright.sync_api', types.SimpleNamespace(sync_playwright=Playwright))
    monkeypatch.setattr(ups_track.time, 'time', lambda: clock[0])
    kwargs = {'browser_channel': browser_channel} if browser_channel else {}
    result = ups_track.track_ups(NUMBER, timeout_nav=1, wait_ms=2000, attempts=1, **kwargs)
    assert result['ok'] is matching_response
    assert result['tracking'] == NUMBER
    assert launches[0].get('channel') == browser_channel
    if not matching_response:
        assert result['error'] == 'UPS response did not contain matching tracking data'


def test_official_402_is_request_rejected_not_missing_shipment_or_captcha():
    result = ups_track.parse_tracking_response(NUMBER, {
        'statusCode': '402', 'statusText': 'Invalid Request', 'trackDetails': None})
    assert result == {'tracking': NUMBER, 'ok': False, 'request_rejected': True,
                      'error': 'UPS official request rejected (code 402: Invalid Request)'}
    assert ups_track.parse_tracking_response(NUMBER, {'statusCode': 'NEW', 'trackDetails': None}) is None


def test_official_request_rejection_is_not_immediately_retried(monkeypatch):
    attempts = []
    rejection = {'tracking': NUMBER, 'ok': False, 'request_rejected': True,
                 'error': 'UPS official request rejected (code 402: Invalid Request)'}
    monkeypatch.setattr(ups_track, '_track_once', lambda *args: attempts.append(args) or rejection)
    monkeypatch.setattr(ups_track.time, 'sleep', lambda seconds: None)
    assert ups_track.track_ups(NUMBER, browser_channel='chrome', attempts=2) == rejection
    assert len(attempts) == 1
