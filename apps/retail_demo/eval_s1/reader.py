"""Readonly JSON compatibility: native null metadata is an empty lookup map.

Raw native JSON evidence remains unchanged; no facts/authorization synthesized,
no scorer condition/threshold changes. Added after the first observation crash.
"""
import copy

def scoring_view(database):
 result=copy.deepcopy(database)
 for row in result['parlant_events']:
  if row['doc'].get('metadata') is None:row['doc']['metadata']={}
 return result
