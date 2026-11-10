"""Browser regression: actual synthetic workflow plus isolated transport-race fixtures.

Requires a running local Assay API, playwright and Chromium. Writes all results
under the supplied Assay artifact directory. Does not call an LLM provider.
"""
import argparse
import asyncio
import json
from pathlib import Path
import uuid
from playwright.async_api import async_playwright


async def real_workflow(browser, base, output):
    page = await browser.new_page(viewport={'width':1440,'height':1100})
    errors=[]
    page.on('pageerror',lambda e: errors.append(str(e)))
    await page.goto(base+'/#/research')
    await page.get_by_label('Dataset',exact=True).locator('option').first.wait_for(state='attached')
    await page.get_by_text('Start a new session',exact=True).click()
    await page.get_by_label('Dataset',exact=True).select_option(label='synthetic-demo')
    goal='Synthetic reversal review '+uuid.uuid4().hex[:8]
    await page.get_by_label('Research goal',exact=True).fill(goal)
    await page.get_by_role('button',name='Create research session',exact=True).click()
    await page.locator('.rw-panel').get_by_text(goal,exact=True).first.wait_for()
    await page.get_by_text('Evaluate a candidate',exact=True).click()
    await page.get_by_label('Experiment hypothesis',exact=True).fill('Intentional invalid operator to inspect diagnostics')
    await page.get_by_label('Factor expression',exact=True).fill('unknown_operator(close)')
    await page.get_by_role('button',name='Evaluate candidate · human action',exact=True).click()
    await page.locator('.rw-attempt.failed').first.wait_for()
    sid=await page.get_by_label('Research session',exact=True).input_value()
    state=await (await page.request.get(base+'/v1/research/sessions/'+sid)).json()
    failed=state['experiments'][0]
    await page.get_by_label('Parent experiment',exact=True).select_option(failed['id'])
    await page.get_by_label('Experiment hypothesis',exact=True).fill('Replace unsupported operator with a five-day reversal expression')
    await page.get_by_label('Factor expression',exact=True).fill('-cs_rank(ts_returns(close,5))')
    await page.get_by_role('button',name='Evaluate candidate · human action',exact=True).click()
    await page.locator('.rw-attempt.completed').first.wait_for()
    await page.get_by_text('Candidate decisions',exact=True).click()
    await page.get_by_label('Candidate decision',exact=True).select_option('accepted')
    await page.get_by_label('Decision reason',exact=True).fill('Retain this synthetic candidate for further research')
    await page.get_by_role('button',name='Save candidate decision',exact=True).click()
    await page.wait_for_function("document.querySelector('[aria-label=\"Decision reason\"]').value === ''")
    await page.get_by_label('Handoff note',exact=True).fill('The invalid expression failed; the revised factor executed. This synthetic test does not establish profitability.')
    await page.get_by_role('button',name='Save handoff note',exact=True).click()
    await page.get_by_label('Handoff note',exact=True).wait_for()
    await page.wait_for_function("document.querySelector('[aria-label=\"Handoff note\"]').value === ''")
    async with page.expect_download() as download_info:
        await page.get_by_role('button',name='Download manifest',exact=True).click()
    download=await download_info.value
    await download.save_as(output/'browser-manifest.json')
    manifest=json.loads((output/'browser-manifest.json').read_text())
    from tempfile import TemporaryDirectory
    from assay.research import ResearchWorkspace
    assert ResearchWorkspace.verify_manifest(manifest), 'Downloaded manifest integrity failed'
    with TemporaryDirectory(dir=output) as temporary:
        imported = ResearchWorkspace(Path(temporary)/'research.sqlite3')
        imported.import_session(manifest)
        assert imported.get_session(sid)['experiments'] == manifest['state']['experiments']
    records=manifest['state']['experiments']
    assert manifest['state']['decisions'][0]['decision']=='accepted'
    assert manifest['state']['decisions'][0]['experiment_id']==records[1]['id']
    assert records[1]['report']['portfolio']['status']=='completed'
    assert [a['status'] for a in records]==['failed','completed']
    assert records[1]['parent_id']==records[0]['id']
    assert all(a['agent']=='human:webui' for a in records)
    await page.reload()
    await page.get_by_label('Research session',exact=True).select_option(sid)
    await page.locator('.rw-attempt.completed').first.wait_for()
    await page.locator('.rw-attempt.completed button').first.click()
    await page.screenshot(path=str(output/'workbench.png'),full_page=True)
    assert not errors,errors
    await page.close()
    return {'session_id':sid,'statuses':[a['status'] for a in records], 'parent_link_verified':True,
        'export_and_reload_verified':True,'manifest_integrity_and_import_verified':True,'page_errors':errors,'provider_calls':0,'execution_mode':'human browser actions on real synthetic evaluator'}


async def race_fixtures(browser, base):
    page=await browser.new_page()
    requests=[]; terminal=False; delay=False; delay_post=False
    post_started=asyncio.Event(); post_release=asyncio.Event()
    started=asyncio.Event(); release=asyncio.Event()
    rows=[{'id':'a','goal':'Session A'},{'id':'b','goal':'Session B'}]
    async def handler(route):
        nonlocal delay
        req=route.request
        path=req.url.split('/v1/research',1)[1]
        if path=='/datasets': value=[{'id':'d','name':'Fixture dataset'}]
        elif path=='/sessions':
            if delay:
                started.set(); await release.wait(); delay=False
            value=rows
        elif path.endswith('/experiments'):
            requests.append(req.post_data_json)
            value={'id':'e','status':'completed' if terminal else 'running'}
            if delay_post:
                post_started.set(); await post_release.wait()
        else:
            identifier=path.split('/')[2]
            value={'id':identifier,'goal':'Session '+identifier,'dataset':{'name':'Fixture dataset'},'budget':5,
                'remaining_experiments':4,'notes':[], 'experiments':[{'id':'e','expr':'close','hypothesis':'fixture',
                'agent':'fixture','status':'completed' if terminal else 'running','report':{}}]}
        await route.fulfill(json=value)
    await page.route('**/v1/research/**',handler)
    await page.goto(base+'/#/research')
    await page.get_by_label('Research session',exact=True).select_option('a')
    await page.get_by_text('Evaluate a candidate',exact=True).click()
    await page.get_by_label('Experiment hypothesis',exact=True).fill('fixture')
    await page.get_by_label('Factor expression',exact=True).fill('close')
    run=page.get_by_role('button',name='Evaluate candidate · human action',exact=True)
    await run.click(); await page.wait_for_function("!Array.from(document.querySelectorAll('button')).find(x => x.textContent.startsWith('Evaluate candidate')).disabled")
    await run.click(); await page.wait_for_function("!Array.from(document.querySelectorAll('button')).find(x => x.textContent.startsWith('Evaluate candidate')).disabled")
    assert len(requests)==2 and requests[0]['request_id']==requests[1]['request_id']
    terminal=True
    await page.get_by_role('button',name='Refresh sessions',exact=True).click()
    await page.locator('.rw-attempt.completed').wait_for()
    await run.click(); await page.wait_for_function("!Array.from(document.querySelectorAll('button')).find(x => x.textContent.startsWith('Evaluate candidate')).disabled")
    assert requests[2]['request_id'] != requests[1]['request_id']
    terminal=False
    await run.click(); await page.wait_for_function("!Array.from(document.querySelectorAll('button')).find(x => x.textContent.startsWith('Evaluate candidate')).disabled")
    delay_post=True
    await run.click(); await asyncio.wait_for(post_started.wait(),10)
    terminal=True
    await page.get_by_role('button',name='Refresh sessions',exact=True).click()
    await page.locator('.rw-attempt.completed').wait_for()
    post_release.set()
    await page.wait_for_function("!Array.from(document.querySelectorAll('button')).find(x => x.textContent.startsWith('Evaluate candidate')).disabled")
    assert requests[3]['request_id']==requests[4]['request_id']
    assert await page.locator('[role=alert]').inner_text()==''
    delay_post=False
    delay=True
    await page.get_by_role('button',name='Refresh sessions',exact=True).click()
    await asyncio.wait_for(started.wait(),10)
    await page.get_by_label('Research session',exact=True).select_option('b')
    release.set()
    await page.get_by_text('Session b',exact=True).wait_for()
    await page.wait_for_timeout(200)
    assert await page.get_by_label('Research session',exact=True).input_value()=='b'
    await page.close()
    return {'running_retry_same_request_id':True,'terminal_allows_new_request':True,'late_list_preserves_new_selection':True,'late_running_response_after_terminal_refresh':True,'execution_mode':'mock transport regression, not model evidence'}


async def main(args):
    args.output.mkdir(parents=True,exist_ok=True)
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=['--no-sandbox'])
        try:
            result={'real_workflow':await real_workflow(browser,args.base_url,args.output),
                    'race_fixtures':await race_fixtures(browser,args.base_url)}
            (args.output/'browser-checks.json').write_text(json.dumps(result,indent=2))
            print(json.dumps(result,indent=2))
        finally:
            await browser.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:8014')
    parser.add_argument('--output',type=Path,default=Path('artifacts/workbench-20261107'))
    asyncio.run(main(parser.parse_args()))
