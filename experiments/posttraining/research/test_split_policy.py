"""No models, APIs or episode execution; V2 isolation remains enforced."""

from copy import deepcopy
import unittest
from .split_policy import new_groups, register, check_texts, fingerprint, check_episode


class SplitTests(unittest.TestCase):
    def test_shared_capability_distinct_templates_pass(self):
        groups = register(new_groups())
        self.assertEqual(len(groups), 6)
        self.assertEqual(len({g["capability_family"] for g in groups}), 1)
        self.assertEqual(len({g["normalized_fingerprint"] for g in groups}), 6)

    def test_renaming_same_template_does_not_change_fingerprint(self):
        group = new_groups()[0]
        clone = deepcopy(group)
        clone["causal_template_group"] = "renamed.valid"
        clone["split"] = "sft_valid"
        clone["seed"] = 987
        clone["product_name"] = "new wording"
        self.assertEqual(fingerprint(group["descriptor"]), fingerprint(clone["descriptor"]))
        with self.assertRaises(ValueError):
            register([group, clone])

    def test_holdout_and_cross_episode_split_fail(self):
        groups = register(new_groups())
        episode = dict(groups[0], template_fingerprint=groups[0]["normalized_fingerprint"])
        self.assertTrue(check_episode(episode, groups))
        episode["split"] = "sft_valid"
        with self.assertRaises(ValueError):
            check_episode(episode, groups)
        groups[0]["reserved_holdout_overlap"] = ["H2"]
        with self.assertRaises(ValueError):
            register(groups)

    def test_instance_numbers_and_ids_do_not_hide_text_duplicate(self):
        with self.assertRaises(ValueError):
            check_texts(
                [
                    {"split": "train", "scenario_texts": ["订单ORD-AA退2件"]},
                    {"split": "sft_valid", "scenario_texts": ["订单ORD-BB退3件"]},
                ]
            )


if __name__ == "__main__":
    unittest.main()
