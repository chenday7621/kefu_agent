# First policy SFT experiment

Status: `SFT_FINALIZATION_BLOCKED`. B0 completed29/30 with one declared missing unit. Independent DATA verified100 TRAIN episodes /40 VALID;1016 TRAIN decisions including254 CALL and254 hard NO_CALL,320 VALID decisions. Original61 completed episodes and516 selected samples preserved.

Separate two-step sanity passed. The single formal epoch reached optimizer step64/max_steps64 and epoch1.0; native checkpoint64 was saved. The custom on_save callback then treated an adjacent checkpoint hash JSON as a checkpoint directory and failed parsing its step number. Final validation, final adapter publication and30-task reevaluation remain incomplete. Cumulative estimated DeepSeek cost USD6.337684496 under USD10 authorization; reevaluation reserve retained.

The minimal unapplied proposal filters actual directories in checkpoint enumeration. Recovery should use the already-complete checkpoint64 for validation/finalization only, with no Trainer.train call, new optimizer steps, repeated sanity, B0 rerun or recollection. The latest DATA-only infrastructure scope did not authorize a trainer callback patch.

Fixed20-task main comparison remains frozen; nine contract-affected B0 tasks and one missing task are separate. Shared business capabilities, concrete template/episode isolation; no clean-test claim. DATA infrastructure fixes are not SFT gains. No second SFT, RL or deployment. Secrets, generated data, weights, databases and full trajectories remain outside Git.
