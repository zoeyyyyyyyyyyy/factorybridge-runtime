import unittest
from schemas import Order,Part
from bench.order_io import parse_order
from bench.session import run_session
from bench.checker import check
from factorybridge_demo import config
from agents.planner import Planner
from marketplace.core import Marketplace
from simulator import World
import llm

class RuntimeTest(unittest.TestCase):
    def test_invalid_quantity(self):
        with self.assertRaises(ValueError):parse_order({'order_id':'unit','deadline_day':8,'budget_usd':100,'parts':[{'part_id':'fixture-spacer','kind':'print','qty':0}]})

    def test_scripted_unit_fixture(self):
        order=Order('fixture-integration-only',8,100,[Part('fixture-spacer','print',2,'PLA',1,10,.2),Part('fixture-board','pcb',1,layers=2,size_mm=[10,10]),Part('fixture-screw','purchased',2,sku='fixture-screw')],injected_events=[{'day':3,'supplier_kind':'print','type':'delay','days':2}])
        world=World.from_config(order,config(),1,0);old=llm.call_llm
        llm.call_llm=lambda *args:{'role':'assistant','content':'{"plan_id":"plan-0","reason":"SCRIPTED UNIT FIXTURE"}'}
        try:result=run_session(order,Planner(),world,Marketplace(world),check=check,checker_identity='bench.checker')
        finally:llm.call_llm=old
        self.assertTrue(result['success']);self.assertEqual(result['truth']['final_delivery_day'],8)
        self.assertEqual(result['truth']['total_cost'],42.5)
        self.assertEqual(result['llm_usage']['llm_calls'],0)

if __name__=='__main__':unittest.main()
