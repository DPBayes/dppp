"""Logistic regression example from numpyro.

"""

import os

# allow example to find dppp without installing
import sys
sys.path.append(os.path.dirname(sys.path[0]))
#### 

import argparse
import time

import matplotlib.pyplot as plt
import numpy as onp

import jax.numpy as np
from jax import jit, lax, random
from jax.experimental import optimizers, stax
from jax.random import PRNGKey
import jax

import numpyro.distributions as dist
from numpyro.handlers import param, sample, seed, trace, substitute

#from dppp.svi import per_example_elbo, svi
from numpyro.svi import elbo, svi

from datasets import batchify_data

def model(k, obs_or_shape):
    """Defines the generative probabilistic model: p(x|z)p(z)

    :param k: number of components in the mixture
    :param d: number of dimensions per data item
    :param N: number of samples (default: 1) (ignored and set to obs.shape[0] if obs is given)
    :param obs: observed samples to condition the model with (default: None)
    """
    # f(x) = sum_k pi_k * phi(x; mu_k, sigma_k^2), where phi denotes Gaussian pdf
    # 	* pi_k ~ Dirichlet(alpha), where alpha \in R_+^k
    # 	* mu_k ~ Normal(0, 1)
    # * sigma_k ~ Gamma(a0, b0), where a0,b0 > 0

    # note(lumip): for now we use a simpler model and fix the sigmas

    if isinstance(obs_or_shape, tuple):
        assert(len(obs_or_shape) == 2)
        N, d = obs_or_shape
        obs = None
    else:
        obs = obs_or_shape
        assert(obs is not None)
        assert(len(obs.shape) <= 2)
        N, d = np.atleast_2d(obs).shape

    alpha = np.ones(k)*0.3
    # a0, b0 = np.ones((k,d))*2., np.ones((k,d))*2.

    pis = np.broadcast_to(sample('pis', dist.Dirichlet(alpha)), (N,k))
    assert(pis.shape == (N, k))
    mus = sample('mus', dist.Normal(np.zeros((k, d)), 1.))
    # sigs = sample('sigmas', dist.Gamma(a0, b0))
    sigs = np.ones((k, d))

    ks = sample('ks', dist.Categorical(pis)).flatten()
    X = sample('obs', dist.Normal(mus[ks], sigs[ks]), obs=obs)
    return X

def compute_assignment_log_posterior(k, obs, mus, sigs, pis_prior):
    N = np.atleast_1d(obs).shape[0]

    ks_log_post = [None] * k
    for j in range(k):
        log_prob_x_zj = np.sum(dist.Normal(mus[j], sigs[j]).log_prob(obs), axis=1).flatten()
        assert(log_prob_x_zj.shape == (N,))
        log_prob_zj = dist.Categorical(pis_prior).log_prob(j)
        log_prob = log_prob_x_zj + log_prob_zj
        ks_log_post[j] = log_prob

    ks_log_post = np.stack(ks_log_post, axis=0)
    return ks_log_post

def estimate_pis(assignment_log_posterior):
    # essentially pis = |E[p(ks)] so we set pis = p(ks)
    # note(lumip): need to get to probabilities from logs probabilities but we
    #   cannot exponentiate directly due to numerical inaccuracy. workaround:
    # pis_post = [None] * k
    # for j in range(k):
    #     log_r = ks_log_post - ks_log_post[j]
    #     r = np.clip(np.exp(log_r), 1e-5, 1e5)
    #     pis_post[j] = 1./np.sum(r, axis=0)
    ks_log_post = assignment_log_posterior
    k = assignment_log_posterior.shape[0]

    pis_post = [None] * k
    for j in range(k):
        log_r = ks_log_post - ks_log_post[j]
        r = np.clip(np.exp(log_r), 1e-5, 1e5)
        pis_post[j] = 1./np.sum(r, axis=0)

    pis_post = np.stack(pis_post, axis=1)
    return pis_post


def guide(k, obs):
    """Defines the probabilistic guide for z (variational approximation to posterior): q(z) ~ p(z|x)

    :param k: number of components in the mixture
    :param obs: observed samples to condition the model with
    """
    assert(obs is not None)
    assert(len(obs.shape) <= 2)
    N, d = np.atleast_2d(obs).shape

    # a0, b0 = param('a0', np.ones((k, d))*2.), param('b0', np.ones((k, d))*2.)
    alpha = param('alpha', np.ones(k)*0.3)
    mus_loc = param('mus_loc', np.zeros((k, d)))
    mus_std = np.exp(param('mus_std_log', np.zeros((k, d))))

    mus = sample('mus', dist.Normal(mus_loc, mus_std))
    # sigs = sample('sigmas', dist.Gamma(a0, b0))
    sigs = np.ones((k, d))

    pis_prior = sample('pis', dist.Dirichlet(alpha))

    # compute posterior probabilities of ks after seeing the data
    ks_log_post = compute_assignment_log_posterior(k, obs, mus, sigs, pis_prior)

    # from ks posterior probabilities to pis
    pis_post = estimate_pis(ks_log_post)
    # we require a ks for each example. ensure that pis is of correct shape
    assert(pis_post.shape == (N, k))

    ks = sample('ks', dist.Categorical(pis_post)).flatten()
    return pis_post, ks, mus, sigs

def create_toy_data(N, k, d):
    ## Create some toy data
    onp.random.seed(122)

    # note(lumip): toy data is imbalanced. the last component has
    #   twice as many samples as the others
    ks = onp.random.randint(0, k+1, N)
    ks[ks == k] = k - 1
    X = onp.zeros((N, d))

    mus = [-10. * onp.ones(d), 10. * onp.ones(d), -2. * onp.ones(d)]
    sigs = [onp.sqrt(0.1), 1., onp.sqrt(0.1)]
    for i in range(k):
        N_i = onp.sum(ks == i)
        X_i = mus[i] + sigs[i] * onp.random.randn(N_i, d)
        X[ks == i] = X_i

    # note(lumip): workaround! np.array( ) of jax 0.1.35 (required by
    #   numpyro 0.1.0) does not transform incoming numpy arrays into its
    #   internal representation, which can lead to an exception being thrown
    #   if any of the affected arrays find their way into a jit'ed function.
    #   This is fixed in the current master branch of jax but due to numpyro
    #   we cannot currently benefit from that.
    # todo: remove device_put once sufficiently high version number of jax is
    #   present
    device_put = jit(lambda x: x)
    X = device_put(X)
    mus = np.array(mus)
    sigs = np.array(sigs)

    latent_vals = (ks, mus, sigs)
    return X, latent_vals

def main(args):
    N = args.num_samples
    k = args.num_components
    d = args.dimensions

    X, latent_vals = create_toy_data(N, k, d)
    train_init, train_fetch = batchify_data((X,), args.batch_size)

    ## Init optimizer and training algorithms
    opt_init, opt_update, get_params = optimizers.adam(args.learning_rate)

    # note(lumip): fix the parameters in the models
    def fix_params(model_fn, k):
        def fixed_params_fn(obs):
            return model_fn(k, obs)
        return fixed_params_fn

    model_fixed = fix_params(model, k)
    guide_fixed = fix_params(guide, k)

    # per_example_loss = per_example_elbo
    # combined_loss = np.sum
    # svi_init, svi_update, svi_eval = svi(
    #     model_fixed, guide_fixed, per_example_loss, combined_loss, opt_init,
    #     opt_update, get_params, per_example_variables={'obs', 'ks'}
    # )

    # note(lumip): use default numpyro svi and elbo for now to get the model
    #   to work
    svi_init, svi_update, svi_eval = svi(
        model_fixed, guide_fixed, elbo, opt_init,
        opt_update, get_params
    )

    svi_update = jit(svi_update)

    rng = PRNGKey(123)

    rng, svi_init_rng = random.split(rng, 2)
    batch = train_fetch(0)
    opt_state = svi_init(svi_init_rng, batch, batch)

    @jit
    def epoch_train(opt_state, rng, data_idx, num_batch):
        def body_fn(i, val):
            loss_sum, opt_state, rng = val
            rng, update_rng = random.split(rng, 2)
            batch = train_fetch(i, data_idx)
            loss, opt_state, rng = svi_update(
                i, opt_state, update_rng, batch, batch
            )
            loss_sum += loss / len(batch[0])
            return loss_sum, opt_state, rng

        loss, opt_state, rng = lax.fori_loop(0, num_batch, body_fn, (0., opt_state, rng))
        loss /= num_batch
        return loss, opt_state, rng

    
    @jit
    def eval_test(opt_state, rng, data_idx, num_batch):
        def body_fn(i, val):
            loss_sum, rng = val
            batch = train_fetch(i, data_idx)
            loss = svi_eval(opt_state, rng, batch, batch) / len(batch[0])
            loss_sum += loss
            return loss_sum, rng

        loss, _ = lax.fori_loop(0, num_batch, body_fn, (0., rng))
        loss = loss / num_batch
        return loss

    with onp.errstate(divide='ignore', invalid='ignore'): # expected divide by zero warning
        smoothed_loss_window = 1./onp.zeros(5)
    window_idx = 0

	## Train model
    for i in range(args.num_epochs):
        t_start = time.time()
        rng, data_fetch_rng, test_rng = random.split(rng, 3)

        num_train, train_idx = train_init(rng=data_fetch_rng)
        _, opt_state, rng = epoch_train(
            opt_state, rng, train_idx, num_train
        )

        if i % 100 == 0:
            # computing loss over training data (for now?)
            test_loss = eval_test(
                opt_state, test_rng, train_idx, num_train
            )
            smoothed_loss_window[window_idx] = test_loss
            smoothed_loss = onp.nanmean(smoothed_loss_window)
            window_idx = (window_idx + 1) % 5

            print("Epoch {}: loss = {}, smoothed loss = {} ({:.2f} s.)".format(
                    i, test_loss, smoothed_loss, time.time() - t_start
                ))

            params = get_params(opt_state)
            print(params)

    params = get_params(opt_state)
    print(params)
    print("MAP estimate of mixture weights: {}".format(dist.Dirichlet(params['alpha']).mean))
    print("MAP estimate of mixture modes  : {} (variance: {})".format(params['mus_loc'], np.exp(params['mus_std_log'])))

    # getting accuracy score for attributing data to the mixture components
    # based on the learned model
    original_assignment = latent_vals[0]
    original_modes = latent_vals[1]
    # we first map our true modes to the ones learned in the model using the
    # log posterior for ks
    mode_assignment_posterior = compute_assignment_log_posterior(k, original_modes, params['mus_loc'], np.ones((k, d)), dist.Dirichlet(params['alpha']).mean)
    mode_map = np.argmax(mode_assignment_posterior, axis=0)._value
    # a potential problem could be that mode_map might not be bijective, skewing
    # the results of the mapping. we build the inverse map and use identity
    # mapping as a base to counter that
    inv_mode_map = {j:j for j in range(k)}
    inv_mode_map.update({mode_map[j]:j for j in range(k)})
    
    # we next obtain the assignments for the data according to the model and
    # pass them through the inverse map we just build
    post_data_assignment = compute_assignment_log_posterior(k, X, params['mus_loc'], np.ones((k, d)), dist.Dirichlet(params['alpha']).mean)
    post_data_assignment = np.argmax(post_data_assignment, axis=0)
    remapped_data_assignment = np.array([inv_mode_map[j] for j in post_data_assignment._value])

    # finally, we can compare the results with the original assigments and compute
    # the accuracy
    acc = np.sum(original_assignment == remapped_data_assignment)/X.shape[0]
    print("assignment accuracy: {}".format(acc))
    
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="parse args")
    parser.add_argument('-n', '--num-epochs', default=10000, type=int, help='number of training epochs')
    parser.add_argument('-lr', '--learning-rate', default=1.0e-3, type=float, help='learning rate')
    parser.add_argument('-batch-size', default=32, type=int, help='batch size')
    parser.add_argument('-d', '--dimensions', default=2, type=int, help='data dimension')
    parser.add_argument('-N', '--num-samples', default=1024, type=int, help='data samples count')
    parser.add_argument('-k', '--num-components', default=3, type=int, help='number of components in the mixture model')
    args = parser.parse_args()
    main(args)
