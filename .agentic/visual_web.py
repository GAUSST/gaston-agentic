"""Managed real Vite application inspection, distinct from formal E2E acceptance."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler

from project_layout import project_state
from platform_compat import popen_kwargs, stop_owned
import web_acceptance as web

SCRIPT = r"""
const fs = require('fs');
const { pathToFileURL } = require('url');
const cfg = JSON.parse(process.argv[1]);
const { chromium } = require(cfg.module);
(async () => {
  let browser;
  const results = [], errors = [], failed = [];
  try {
    browser = await chromium.launch({ headless: !cfg.visible });
    const context = await browser.newContext({ serviceWorkers: 'block' });
    const origins = new Set([new URL(cfg.base).origin, ...(cfg.api ? [new URL(cfg.api).origin] : [])]);
    await context.route('**/*', route => {
      const u = new URL(route.request().url());
      return origins.has(u.origin) || ['data:', 'blob:'].includes(u.protocol) ? route.continue() : route.abort('blockedbyclient');
    });
    const page = await context.newPage();
    page.on('pageerror', err => errors.push(String(err).slice(0, 500)));
    page.on('requestfailed', req => failed.push({ url: req.url().split('?')[0], reason: req.failure()?.errorText }));
    if (cfg.setup) {
      const { setup } = await import(pathToFileURL(cfg.setup).href);
      if (typeof setup !== 'function') throw new Error('visual_setup must export async setup');
      await setup({ page, context, baseURL: cfg.base, apiBaseURL: cfg.api });
    }
    for (const route of cfg.routes) {
      for (const width of cfg.widths) {
        await page.setViewportSize({ width, height: cfg.height });
        const target = new URL(route, cfg.base);
        const response = await page.goto(target.href, { waitUntil: 'domcontentloaded', timeout: 30000 });
        if (!response || !response.ok() || new URL(page.url()).origin !== new URL(cfg.base).origin) {
          throw new Error('Application navigation failed: ' + route + ' / HTTP ' + (response?.status() ?? 'none'));
        }
        await page.locator('body').waitFor({ state: 'visible', timeout: 10000 });
        await page.evaluate(() => document.fonts.ready);
        const file = 'route-' + cfg.routes.indexOf(route) + '-' + width + '.png';
        await page.screenshot({ path: cfg.output + '/' + file, animations: 'disabled', timeout: 15000 });
        results.push({ route, width, height: cfg.height, file, finalPath: new URL(page.url()).pathname, status: response.status() });
      }
    }
    await context.close();
  } catch (err) {
    errors.push(String(err).slice(0, 1000));
  } finally {
    if (browser) await browser.close();
    fs.writeFileSync(cfg.output + '/report.json', JSON.stringify({ results, errors, failed }, null, 2));
  }
  process.exitCode = errors.length || results.length !== cfg.routes.length * cfg.widths.length ? 1 : 0;
})().catch(e => { console.error(String(e)); process.exitCode = 1; });
"""


def _contract(root, project, routes, widths, height, visible):
    folder = web._project(root, project)
    if type(visible) is not bool:
        raise ValueError('visible must be boolean')
    if visible and os.name != 'nt' and __import__('platform').system() != 'Darwin' and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        raise ValueError('No desktop display; use headless visual inspection')
    if not isinstance(routes, list) or not 1 <= len(routes) <= 8 or len(set(routes)) != len(routes):
        raise ValueError('Supply one to eight distinct local routes')
    for route in routes:
        if not isinstance(route, str) or not route.startswith('/') or route.startswith('//') or '\\' in route or urlsplit(route).netloc or any(ord(ch) < 32 for ch in route):
            raise ValueError('Only local application routes are supported')
    if not isinstance(widths, list) or not 1 <= len(widths) <= 5 or len(set(widths)) != len(widths) or any(type(x) is not int or not 300 <= x <= 2400 for x in widths):
        raise ValueError('Supply one to five distinct widths from 300 to 2400')
    if type(height) is not int or not 400 <= height <= 1800:
        raise ValueError('height must be from 400 to 1800')
    node = shutil.which('node')
    vite = folder/'node_modules/vite/bin/vite.js'
    if not node or not vite.is_file():
        raise ValueError('Visual runner requires existing local Vite and Node; no automatic installation')
    module = folder/'node_modules/@playwright/test'
    if not (module/'package.json').is_file():
        import playwright_tools
        module = playwright_tools.test_workspace(root)/'node_modules/@playwright/test'
    if not (module/'package.json').is_file():
        raise ValueError('Installed Playwright Test/Chromium is required; no automatic download')
    descriptor = folder/'gaston-web-e2e.json'
    cfg = json.loads(descriptor.read_text(encoding='utf-8')) if descriptor.is_file() else {}
    if descriptor.is_symlink() or not isinstance(cfg, dict):
        raise ValueError('Invalid visual application descriptor')
    backend = web._backend_for(root, folder)
    setup = None
    if cfg.get('visual_setup') is not None:
        raw=cfg['visual_setup']
        if not isinstance(raw,str) or Path(raw).is_absolute() or '..' in Path(raw).parts:
            raise ValueError('visual_setup must be a project-relative test module')
        source=folder/raw
        setup=source.resolve()
        if not setup.is_relative_to(folder) or source.is_symlink() or setup.suffix not in ('.mjs','.js') or not setup.is_file() or not set(setup.relative_to(folder).parts)&{'e2e','tests','playwright'}:
            raise ValueError('visual_setup must be an existing project-owned test module')
    api_env=cfg.get('api_env')
    if api_env is not None and (not isinstance(api_env,str) or not re.fullmatch(r'VITE_[A-Z0-9_]+',api_env)):
        raise ValueError('api_env must name the explicit VITE_* public API URL variable')
    return folder,node,vite,module,backend,setup,api_env


def run(root,project,*,routes=None,widths=None,height=900,visible=False):
    root=Path(root).resolve()
    routes=['/'] if routes is None else routes
    widths=[1440,820,390] if widths is None else widths
    try:
        folder,node,vite,module,backend,setup,api_env=_contract(root,project,routes,widths,height,visible)
        local=project_state(root)/'local/visual-inspections'
        local.mkdir(mode=0o700,parents=True,exist_ok=True)
        if local.is_symlink() or not local.resolve().is_relative_to(project_state(root).resolve()):
            raise ValueError('Unsafe inspection evidence directory')
        output=Path(tempfile.mkdtemp(prefix='web-',dir=local))
        from platform_compat import protect_private_path
        protect_private_path(output)
    except (OSError,ValueError,TypeError,AttributeError) as exc:
        return {'status':'blocked','project':project,'reason':str(exc)}

    def inspect(api=None):
        env=web._environment()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        env.update(PLAYWRIGHT_BASE_URL=base,GASTON_WEB_PORT=str(port))
        if api:
            env.update(GASTON_API_BASE_URL=api,AGENTIC_TEST_API_BASE_URL=api)
            if api_env:env[api_env]=api
        result={'status':'blocked','project':project,'visual_only':True,'frontend_url':base,
                'backend_url':api,'authenticated_fixture':bool(setup),'artifacts':output.relative_to(root).as_posix()}
        frontend=browser=None
        try:
            with (output/'frontend.log').open('wb') as log:
                frontend=subprocess.Popen([node,str(vite),'--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=folder,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,**popen_kwargs())
                opener=build_opener(ProxyHandler({}))
                deadline=time.monotonic()+60
                ready=False
                while frontend.poll() is None and time.monotonic()<deadline:
                    try:
                        with opener.open(base+'/@vite/client',timeout=2) as response:
                            ready=response.status==200 and 'javascript' in response.headers.get('Content-Type','')
                        if ready and frontend.poll() is None:break
                    except (OSError,ValueError):pass
                    time.sleep(.2)
                if not ready or frontend.poll() is not None:
                    result['reason']='Owned Vite application did not become ready; inspect private frontend.log'
                    return result
                cfg={'module':str(module.resolve()),'base':base,'api':api,'setup':str(setup) if setup else None,
                     'routes':routes,'widths':widths,'height':height,'visible':visible,'output':str(output)}
                with (output/'browser.log').open('wb') as blog:
                    browser=subprocess.Popen([node,'-e',SCRIPT,json.dumps(cfg)],cwd=folder,env=env,stdin=subprocess.DEVNULL,stdout=blog,stderr=subprocess.STDOUT,**popen_kwargs())
                    code=browser.wait(timeout=240)
                report=json.loads((output/'report.json').read_text(encoding='utf-8'))
                captures=report.get('results',[])
                complete=len(captures)==len(routes)*len(widths) and all(
                    re.fullmatch(r'route-\d+-\d+\.png',x.get('file','')) and (output/x['file']).is_file() and (output/x['file']).stat().st_size>0 for x in captures)
                result.update(status='pass' if code==0 and complete and not report.get('errors') else 'fail',
                              screenshots=[{**x,'path':(output/x['file']).relative_to(root).as_posix()} for x in captures if re.fullmatch(r'route-\d+-\d+\.png',x.get('file',''))],
                              page_error_count=len(report.get('errors',[])),request_failure_count=len(report.get('failed',[])),
                              note='Actual application screenshots, not formal E2E acceptance. Inspect artifacts and real test contracts.')
        except (OSError,ValueError,TypeError,subprocess.TimeoutExpired) as exc:
            result.update(status='blocked',reason='Visual runner failed: '+type(exc).__name__+'; inspect private logs')
        finally:
            try:
                for proc in (browser,frontend):
                    if proc is not None:stop_owned(proc,isolated_group=True)
                result['process_cleanup']='complete'
            except (OSError,RuntimeError,subprocess.TimeoutExpired):
                result.update(status='blocked',process_cleanup='failed',reason='Owned process cleanup not confirmed')
        return result

    if backend:
        import managed_backend
        result=managed_backend.run(root,backend,browser_check=inspect)
        return {**result,'project':project,'visual_only':True,'backend_project':backend}
    return inspect()
