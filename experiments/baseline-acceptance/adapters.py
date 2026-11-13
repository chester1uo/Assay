"""Read-only baseline integration for isolated Assay acceptance runs.

The five AlphaBench bridges are ported strategies. AlphaAgent runs its actual
SearchOnlyLoop with injected transport/evaluator. No formal V2 run is resumed.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace


METHODS = ['rdagent', 'quantaalpha', 'factorminer', 'alphamaster', 'mcts', 'alphaagent', 'alpha101']
SOURCE_NAMES = dict(rdagent='RD-Agent',quantaalpha='QuantaAlpha',factorminer='FactorMiner',
                    alphamaster='AlphaMaster',mcts='MCTS-factors',alphaagent='AlphaAgent',alpha101='WorldQuant_alpha101_code')


def spec_from(tool):
    from alphabench_v2.assay import EvaluationSpec
    return EvaluationSpec(tool['universe'],(tool['period_start'],tool['period_end']),tool['as_of'],(1,),
                          tool['universe_as_of'],tool['execution'],tool['adj'])


class NativeAssayEvaluator:
    """Native-loop evaluator with checkpointable settings and a rebound transport.

    A live MCP session cannot be pickled. Native checkpoints retain the tool
    specification; callers must bind a new gateway after loading a checkpoint.
    """
    def __init__(self,gateway,tool):
        self.gateway,self.tool=gateway,tool

    def __getstate__(self):
        return dict(tool=self.tool,gateway=None)

    def bind(self,gateway):
        self.gateway=gateway

    def _gateway(self):
        if self.gateway is None:
            raise RuntimeError('Bind a live Assay MCP gateway after loading the native checkpoint')
        return self.gateway

    def validate_expression(self,expr):
        from alphaagent.app.qlib_rd_loop.alphabench_v2_assay_tools import _v2_policy_error
        result=self._gateway().lint(expr)
        if not result.get('diagnostics',{}).get('ok'):
            return 'Assay syntax validation failed'
        return _v2_policy_error(result,'dsl_v1')

    def evaluate_batch(self,factors,parallel=True):
        from alphaagent.scenarios.qlib.developer.ffo_evaluator import FactorEvalResult
        rows=[]
        for f in factors:
            report=self._gateway().evaluate(f['expression'],spec_from(self.tool))
            ok=not report.get('failure_mode') and report.get('rank_ic') is not None
            rows.append(FactorEvalResult(factor_name=f.get('name','factor'),expression=f['expression'],success=ok,
                ic=report.get('ic') or 0.,rank_ic=report.get('rank_ic') or 0.,icir=report.get('icir') or 0.,
                n_dates=report.get('n_dates') or 0,turnover=report.get('turnover_1d') or 0.,
                metrics={k:report.get(k) or 0. for k in ('rank_ic','rank_icir','ic','icir')},
                portfolio_metrics=report.get('portfolio'),error=report.get('failure_mode') or ''))
        return rows


def run_ported(name, provider, gateway, root, prompt, tool, output, config):
    common=dict(prompt=prompt,model=config['model'],seed=7,temperature=.7,max_tokens=8192,tool_spec=tool)
    if name=='rdagent':
        from alphabench_v2.rdagent_participant import RDAgentCellPipeline,RDAgentFormalConfig
        engine=RDAgentCellPipeline(provider=provider,assay=gateway,source_root=root/'RD-Agent',
                                  config=RDAgentFormalConfig(iterations=2,dsl_mode='dsl_v1'))
        raw,meta=engine.run(**common)
    elif name=='quantaalpha':
        from alphabench_v2.quantaalpha_participant import QuantaAlphaParticipant
        engine=QuantaAlphaParticipant(provider=provider,assay=gateway,project_root=root/'QuantaAlpha',dsl_mode='dsl_v1')
        raw,meta=engine.complete(**common,agent_run_dir=output)
        # This bridge proposes and lints; the harness then evaluates its returned
        # set. It does not pretend QuantaAlpha's full evolution loop was run.
    elif name=='factorminer':
        from alphabench_v2.factorminer_llm_participant import FactorMinerLLMCellSearch,FactorMinerLLMFormalConfig
        class Search(FactorMinerLLMCellSearch):
            @staticmethod
            def _spec(tool_spec):
                # The original bridge's localhost:8002 restriction belongs to
                # formal V2. This isolated test injects MCP, without any HTTP fallback.
                return spec_from(tool_spec)
        engine=Search(provider=provider,assay=gateway,source_root=root/'FactorMiner',provider_name='nebius',
            protocol='isolated_assay_acceptance',config=FactorMinerLLMFormalConfig(iterations=2,population_size=5,
            population_request_mode='batched_population',recovery_rounds=0,dsl_mode='dsl_v1'))
        raw,meta=engine.run(**common,old_expressions=None,response_contract='generation')
    elif name=='alphamaster':
        from alphabench_v2.alphamaster_participant import AlphaMasterCellSearch,AlphaMasterFormalConfig
        engine=AlphaMasterCellSearch(provider=provider,assay=gateway,seed=7,config=AlphaMasterFormalConfig())
        raw,meta=engine.run(**common)
    elif name=='mcts':
        from alphabench_v2.mcts_participant import MCTSCellSearch,MCTSFormalConfig
        engine=MCTSCellSearch(provider=provider,assay=gateway,seed=7,config=MCTSFormalConfig(iterations=2))
        raw,meta=engine.run(**common,response_contract='generation',old_expressions=None)
    else:
        raise ValueError(name)
    trace={k:getattr(engine,k) for k in ('calls','evaluations','rounds','proposals','source_fingerprints') if hasattr(engine,k)}
    (output/'baseline-trace.json').write_text(json.dumps(trace,indent=2,default=str))
    (output/'baseline-response.json').write_text(json.dumps(dict(raw_response=raw,metadata=meta),indent=2,default=str))
    return json.loads(raw)['expressions'],dict(kind='ported_strategy',
        complete_native_pipeline=False,proposal_only=name=='quantaalpha',metadata=meta)


def run_native_alphaagent(provider,gateway,root,prompt,tool,output,config):
    from alphaagent.core.conf import RD_AGENT_SETTINGS
    RD_AGENT_SETTINGS.log_trace_path=str(output/'native-logs')
    from alphaagent.log import logger
    logger.set_trace_path(output/'native-logs')
    from alphaagent.oai.llm_utils import collect_llm_record,pop_llm_records
    from alphaagent.app.qlib_rd_loop.alphabench_v2_agent_participant import _native_dsl_contract,_select_final_five
    from alphaagent.app.qlib_rd_loop.conf import SEARCH_ONLY_PROP_SETTING
    from alphaagent.components.workflow.search_only_loop import SearchOnlyLoop
    from alphaagent.scenarios.qlib.developer.ffo_evaluator import FFOConfig

    class Transport:
        def __init__(self,*args,**kwargs): pass
        def build_messages_and_create_chat_completion(self,user_prompt,system_prompt=None,**kwargs):
            messages=([{'role':'system','content':system_prompt}] if system_prompt else [])+[{'role':'user','content':user_prompt}]
            raw,meta=provider.messages(messages,json_mode=bool(kwargs.get('json_mode')))
            collect_llm_record(dict(model=config['model'],usage=meta.get('usage',{}),tag='native_search',response=raw))
            return raw
    for module in ('alphaagent.scenarios.qlib.proposal.factor_proposal','alphaagent.scenarios.qlib.developer.feedback'):
        importlib.import_module(module).APIBackend=Transport

    evaluator=NativeAssayEvaluator(gateway,tool)
    loop=SearchOnlyLoop(PROP_SETTING=SEARCH_ONLY_PROP_SETTING,
        potential_direction=prompt+'\n\n'+_native_dsl_contract('dsl_v1'),
        ffo_config=FFOConfig(ffo_server='disabled.invalid'),save_dir=str(output/'native-loop'),
        seed_factors=[],round_timeout_seconds=300)
    loop.evaluator=evaluator
    loop.factor_constructor.minimum_factor_count=5
    loop.factor_constructor.alphabench_dsl_mode='dsl_v1'
    loop.factor_constructor.exact_expression_dedup_only=True
    loop.factor_constructor.expression_constraint=evaluator.validate_expression
    loop.trace.scen._background+='\n\n'+_native_dsl_contract('dsl_v1')
    pop_llm_records()
    loop.run(step_n=8)
    pool=loop.get_factor_pool()
    expressions,orientations=_select_final_five(pool)
    (output/'native-factor-pool.json').write_text(json.dumps(pool,indent=2,default=str))
    return expressions,dict(kind='native_search_loop',complete_native_pipeline=False,
        component='alphaagent.components.workflow.search_only_loop.SearchOnlyLoop',rounds_completed=len(loop.round_results),
        replaced_components=['provider_transport','evaluator'],orientations=orientations)


def alpha101_parity(source_root,store,period):
    """Compare a declared PIT-safe subset of the original pandas implementation."""
    import importlib.util
    import numpy as np
    import pandas as pd
    from assay.engine import FactorEngine
    path=source_root/'WorldQuant_alpha101_code/101Alpha_code_1.py'
    spec=importlib.util.spec_from_file_location('original_alpha101_subset',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine=FactorEngine.from_store(store,'NASDAQ100',tuple(period),as_of=period[1],adj='split')
    frame=lambda field:pd.DataFrame(engine.field_matrix(field),index=engine.dates,columns=engine.symbols)
    # The original constructor uses vendor volume units ×100; reverse only that
    # unit conversion. Unused amount/returns placeholders never enter this subset.
    inputs={f'S_DQ_{f.upper()}':frame(f) for f in ('open','high','low','close')}
    inputs.update(S_DQ_VOLUME=frame('volume')/100,S_DQ_PCTCHANGE=frame('close')*np.nan,S_DQ_AMOUNT=frame('close')*np.nan)
    original=module.Alphas(inputs)
    cases={'alpha012':'mul(neg(sign(ts_delta(volume,1))),ts_delta(close,1))',
           'alpha101':'div(sub(close,open),add(sub(high,low),0.001))'}
    result={}
    for name,expr in cases.items():
        expected=getattr(original,name)().to_numpy(dtype=float)
        if engine.membership is not None:
            expected=np.where(engine.membership,expected,np.nan)
        actual=engine.evaluate(expr).values
        np.testing.assert_allclose(actual,expected,equal_nan=True,rtol=1e-12,atol=1e-12)
        finite=np.isfinite(expected)&np.isfinite(actual)
        result[name]=dict(expression=expr,compared_values=int(finite.sum()),max_abs_difference=float(np.max(abs(expected[finite]-actual[finite]))))
    return list(cases.values()),dict(kind='native_formula_subset_parity',cases=result,
        excluded='Original time-axis rank/scale and backfill-based formulas are not validated as PIT-safe.')
