"""Public synthetic supplier fixture; no benchmark or physical production data."""
from importlib.resources import files

def config():
    entries=[]
    for kind,price,lead in [('print',12,4),('pcb',8,2),('purchased',5,1),('shipping',3,2)]:
        for label,extra,delta in [('cheap',0,0),('fast',10,-1)]:
            entry={'supplier_id':f'fixture_{kind}_{label}','kind':kind,
                   'materials':['PLA'] if kind=='print' else [],'lead_days':max(1,lead+delta),
                   'capacity_per_day':10,'advertised_on_time_rate':1,'actual_on_time_rate':1,
                   'defect_rate':0,'price_jitter_fraction':0,
                   'notes':'ARBITRARY SYNTHETIC TEST CONSTANTS; no market meaning'}
            if kind=='print':
                del entry['lead_days'];del entry['capacity_per_day']
                entry['fixture_only']=True
                entry['saved_estimates_file']=str(files('factorybridge_fixture_data').joinpath('print-estimates.synthetic.json'))
                fingerprint=('a' if label=='cheap' else 'b')*64
                entry['print_capacity']={'printers':[{'printer_id':'synthetic-printer','operating_hours_per_day':24,
                    'window_start_hour':0,'profile_sha256':fingerprint,'settings_sha256':fingerprint}],
                    'setup_seconds_per_plate':0,'handling_hours':0,'supplier_to_hub_hours':24}
                entry['pricing']={'setup_usd':price+extra,'per_cm3_usd':0}
                entry['limits']={'max_dim_mm':100,'min_tolerance_mm':.1}
            elif kind=='pcb':
                entry['pricing']={'setup_usd':price+extra,'per_board_usd':0}
                entry['limits']={'max_size_mm':[100,100],'supported_layers':[2,4],'min_tolerance_mm':.1}
            elif kind=='purchased':entry['pricing']={'handling_usd':0,'sku_prices_usd':{'fixture-screw':(price+extra)/2}}
            else:entry['pricing']={'flat_usd':price+extra}
            entries.append(entry)
    return {'fixture_only':True,'price_origin':'simulation_assumptions','provenance':'P3 integration fixture ONLY','suppliers':entries}
