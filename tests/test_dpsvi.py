""" tests that the AdaDP optimizer works correctly
"""
import unittest

from functools import reduce

import jax.numpy as np
import jax
import numpy as onp
from numpyro.infer.svi import SVIState

from dppp.svi import DPSVI, TunableSVI

class DPSVITest(unittest.TestCase):

    def setUp(self):
        self.rng = jax.random.PRNGKey(9782346)
        self.batch_size = 10
        self.px_grads = ((
            np.zeros((self.batch_size, 1000)),
            np.zeros((self.batch_size, 1000))
        ))
        self.px_grads_list, self.tree_def = jax.tree_flatten(self.px_grads)
        self.px_loss = np.arange(self.batch_size, dtype=np.float32)
        self.dp_scale = 1.
        self.svi = DPSVI(None, None, None, None, 1., self.dp_scale)

    def test_dp_noise_perturbation(self):
        svi_state = SVIState(None, self.rng)

        new_svi_state, loss_val, grads = \
            self.svi._combine_and_transform_gradient(
                svi_state, self.px_grads_list, self.px_loss, self.tree_def
            )

        self.assertIs(svi_state.optim_state, new_svi_state.optim_state)
        self.assertFalse(np.allclose(svi_state.rng_key, new_svi_state.rng_key))
        self.assertEqual(np.mean(self.px_loss), loss_val)
        self.assertEqual(self.tree_def, jax.tree_structure(grads))

        for site in jax.tree_leaves(grads):
            self.assertTrue(
                np.allclose(self.dp_scale/self.batch_size, np.std(site), atol=1e-2)
            )
            self.assertTrue(np.allclose(0., np.mean(site), atol=1e-2))

    def test_dp_noise_perturbation_not_deterministic_over_calls(self):
        svi_state = SVIState(None, self.rng)
        new_svi_state, _, first_grads = \
            self.svi._combine_and_transform_gradient(
                svi_state, self.px_grads_list, self.px_loss, self.tree_def
            )

        _, _, second_grads = \
            self.svi._combine_and_transform_gradient(
                new_svi_state, self.px_grads_list, self.px_loss, self.tree_def
            )

        some_gradient_noise_is_equal = reduce(lambda are_equal, acc: are_equal or acc, 
            jax.tree_leaves(
                jax.tree_multimap(
                    lambda x, y: np.allclose(x, y), first_grads, second_grads
                )
            )
        )
        self.assertFalse(some_gradient_noise_is_equal)

    def test_dp_noise_perturbation_not_deterministic_over_sites(self):
        svi_state = SVIState(None, self.rng)
        _, _, grads = \
            self.svi._combine_and_transform_gradient(
                svi_state, self.px_grads_list, self.px_loss, self.tree_def
            )

        noise_sites = jax.tree_leaves(grads)

        self.assertFalse(np.allclose(noise_sites[0], noise_sites[1]))


if __name__ == '__main__':
    unittest.main()