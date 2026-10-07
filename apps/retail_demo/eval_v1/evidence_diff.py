"""Read-only saved-database differences; no new database/model access."""
import json
from pathlib import Path
from .common import config,save


def key(table,row):
 if table=='session_operations':return row['session_id']+':'+row['operation_id']
 for field in ('id','version','singleton','session_id'):
  if field in row:return str(row[field])
 raise ValueError('No recorded primary key: '+table)


def main():
 r=Path(config()['result_dir']);count=0
 for group in ('tasks','faults'):
  for folder in (r/group).iterdir():
   a=folder/'initial_database.json';b=folder/'final_database.json'
   if not a.exists() or not b.exists():continue
   before=json.loads(a.read_text());after=json.loads(b.read_text());delta={}
   for table,old_rows in before.items():
    old={key(table,x):x for x in old_rows};new={key(table,x):x for x in after[table]}
    delta[table]={'before_count':len(old),'after_count':len(new),'added_keys':sorted(new.keys()-old.keys()),'removed_keys':sorted(old.keys()-new.keys()),'changed':{k:[field for field in new[k] if new[k][field]!=old[k].get(field)] for k in old.keys()&new.keys() if old[k]!=new[k]}}
   save(folder/'database_diff.json',{'raw_before':'initial_database.json','raw_after':'final_database.json','changes':delta});count+=1
 print('Saved database differences:',count)

if __name__=='__main__':main()
