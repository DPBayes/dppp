"""Logistic regression example.
"""

import os

# allow example to find dppp without installing
import sys
sys.path.append(os.path.dirname(sys.path[0]))
#### 

import argparse
import time

import numpy as onp

import jax
import jax.numpy as np
from jax import jit, lax, random
from jax.experimental import optimizers
from jax.random import PRNGKey

import numpyro.distributions as dist
from numpyro.handlers import seed, substitute
from numpyro.primitives import param, sample
from numpyro.svi import elbo

from dppp.util import example_count, normalize
from dppp.svi import dpsvi, minibatch

from datasets import batchify_data
from example_util import sigmoid


def model(batch_X, batch_y=None, num_obs_total=None):
    """Defines the generative probabilistic model: p(y|z,X)p(z)

    The model is conditioned on the observed data
    :param batch_X: a batch of predictors
    :param batch_y: a batch of observations
    """
    # note(lumip): the following if construct is currently necessary because
    #   the per-example value_and_grad function uses vmap internally, applying
    #   model and guide to 1-example batches (and stripping the first dimension)
    #   this is not nice because it means that model/guide have to be adapted
    #   if they do the kind of checks as below..
    if np.ndim(batch_X) == 2:
        batch_size = example_count(batch_X)
        assert(batch_y is None or example_count(batch_X) == example_count(batch_y))
    elif np.ndim(batch_X) == 1:
        batch_size = 1
        assert(batch_y is None or example_count(batch_y) == 1)

    z_dim = np.atleast_2d(batch_X).shape[1]

    z_w = sample('w', dist.Normal(np.zeros((z_dim,)), np.ones((z_dim,)))) # prior is N(0,I)
    z_intercept = sample('intercept', dist.Normal(0,1)) # prior is N(0,1)
    logits = batch_X.dot(z_w)+z_intercept

    with minibatch(batch_size, num_obs_total=num_obs_total):
        return sample('obs', dist.Bernoulli(logits=logits), obs=batch_y)#, sample_shape=(batch_size,))


def guide(batch_X, batch_y=None, num_obs_total=None):
    """Defines the probabilistic guide for z (variational approximation to posterior): q(z) ~ p(z|x)
    """
    # we are interested in the posterior of w and intercept
    # since this is a fairly simple model, we just initialize them according
    # to our prior believe and let the optimization handle the rest
    z_dim = np.atleast_2d(batch_X).shape[1]

    z_w_loc = param("w_loc", np.zeros((z_dim,)))
    z_w_std = np.exp(param("w_std_log", np.zeros((z_dim,))))
    z_w = sample('w', dist.Normal(z_w_loc, z_w_std))

    z_intercept_loc = param("intercept_loc", 0.)
    z_interpet_std = np.exp(param("intercept_std_log", 0.))
    z_intercept = sample('intercept', dist.Normal(z_intercept_loc, z_interpet_std))

    return (z_w, z_intercept)


def create_toy_data(N, d):
    ## Create some toy data
    onp.random.seed(123)

    w_true = np.array(onp.random.randn(d))
    intercept_true = np.array(onp.random.randn())

    X = np.array(onp.random.randn(2*N, d))

    logits_true = X.dot(w_true)+intercept_true
    y = 1.*(sigmoid(logits_true)>onp.random.rand(2*N))
    # y = 1.*(logits_true > 0.)

    # note(lumip): workaround! np.array( ) of jax 0.1.37 does not necessarily
    #   transform incoming numpy arrays into its
    #   internal representation, which can lead to an exception being thrown
    #   if any of these arrays find their way into a jit'ed function.
    #   This is fixed in the current master branch of jax but due to numpyro
    #   we cannot currently benefit from that.
    # todo: remove device_put once sufficiently high version number of jax is
    #   present
    X = jax.device_put(X)
    w_true = jax.device_put(w_true)
    intercept_true = jax.device_put(intercept_true)

    X_train = X[:N]
    y_train = y[:N]
    X_test = X[N:]
    y_test = y[N:]

    return (X_train, y_train), (X_test, y_test), (w_true, intercept_true)


def estimate_accuracy_fixed_params(X, y, w, intercept, rng, num_iterations=1):
    rngs = jax.random.split(rng, num_iterations)
    fixed_model = substitute(model, {'w': w, 'intercept': intercept})

    def body_fn(rng):
        y_test = seed(fixed_model, rng)(X)
        hits = (y_test == y)
        accuracy = np.average(hits)
        return accuracy

    accuracies = jax.vmap(body_fn)(rngs)
    return np.average(accuracies)


def estimate_accuracy(X, y, params, rng, num_iterations=1):

    rngs = jax.random.split(rng, num_iterations)

    def body_fn(rng):
        w_rng, b_rng, acc_rng = jax.random.split(rng, 3)

        w = dist.Normal(params['w_loc'], np.exp(params['w_std_log'])).sample(w_rng)
        b = dist.Normal(params['intercept_loc'], np.exp(params['intercept_std_log'])).sample(b_rng)
        y_test = substitute(seed(model, acc_rng), {'w': w, 'intercept': b})(X)
        hits = (y_test == y)
        accuracy = np.average(hits)
        return accuracy

    accuracies = jax.vmap(body_fn)(rngs)
    return np.average(accuracies)

def main(args):
    train_data, test_data, true_params = create_toy_data(
        args.num_samples, args.dimensions
    )

    train_init, train_fetch = batchify_data(train_data, args.batch_size)
    test_init, test_fetch = batchify_data(test_data, args.batch_size)

    ## Init optimizer and training algorithms
    opt_init, opt_update, get_params = optimizers.adam(args.learning_rate)

    rng = PRNGKey(123)
    rng, dp_rng = random.split(rng, 2)

    # note(lumip): value for c currently completely made up
    #   value for dp_scale completely made up currently.
    svi_init, svi_update, svi_eval = dpsvi(
        model, guide, elbo, opt_init, opt_update, get_params,
        rng=dp_rng, dp_scale=0.01, num_obs_total=args.num_samples,
        clipping_threshold=20.
    )

    svi_update = jit(svi_update)

    rng, svi_init_rng, data_fetch_rng = random.split(rng, 3)
    _, train_idx = train_init(rng=data_fetch_rng)
    batch_X, batch_Y = train_fetch(0, train_idx)
    opt_state, _ = svi_init(svi_init_rng, (batch_X, batch_Y), (batch_X, batch_Y))

    @jit
    def epoch_train(rng, opt_state, data_idx, num_batch):
        def body_fn(i, val):
            loss, opt_state, rng = val
            rng, update_rng = random.split(rng, 2)
            batch = train_fetch(i, data_idx)

            batch_loss, opt_state, rng = svi_update(
                i, update_rng, opt_state, batch, batch,
            )
            loss += batch_loss / (args.num_samples * num_batch)
            return loss, opt_state, rng

        return lax.fori_loop(0, num_batch, body_fn, (0., opt_state, rng))

    @jit
    def eval_test(rng, opt_state, data_idx, num_batch):
        params = get_params(opt_state)

        def body_fn(i, val):
            loss_sum, acc_sum, rng = val

            batch = test_fetch(i, data_idx)
            rng, eval_rng, acc_rng = jax.random.split(rng, 3)

            loss = svi_eval(eval_rng, opt_state, batch, batch)
            loss_sum += loss / (args.num_samples * num_batch)

            acc = estimate_accuracy(batch_X, batch_Y, params, acc_rng, 1)
            acc_sum += acc / num_batch

            return loss_sum, acc_sum, rng

        loss, acc, _ = lax.fori_loop(0, num_batch, body_fn, (0., 0., rng))
        return loss, acc

	## Train model
    for i in range(args.num_epochs):
        t_start = time.time()
        rng, train_rng, data_fetch_rng = random.split(rng, 3)

        num_train_batches, train_idx = train_init(rng=data_fetch_rng)
        train_loss, opt_state, _ = epoch_train(
            train_rng, opt_state, train_idx, num_train_batches
        )

        if (i % (args.num_epochs//10)) == 0:
            rng, test_rng, test_fetch_rng = random.split(rng, 3)
            num_test_batches, test_idx = test_init(rng=test_fetch_rng)
            test_loss, test_acc = eval_test(
                test_rng, opt_state, test_idx, num_test_batches
            )
            print("Epoch {}: loss = {}, acc = {} (loss on training set: {}) ({:.2f} s.)".format(
                i, test_loss, test_acc, train_loss, time.time() - t_start
            ))

    # parameters for logistic regression may be scaled arbitrarily. normalize
    #   w (and scale intercept accordingly) for comparison
    w_true = normalize(true_params[0])
    scale_true = np.linalg.norm(true_params[0])
    intercept_true = true_params[1] / scale_true

    params = get_params(opt_state)
    w_post = normalize(params['w_loc'])
    scale_post = np.linalg.norm(params['w_loc'])
    intercept_post = params['intercept_loc'] / scale_post

    print("w_loc: {}\nexpected: {}\nerror: {}".format(w_post, w_true, np.linalg.norm(w_post-w_true)))
    print("w_std: {}".format(np.exp(params['w_std_log'])))
    print("")
    print("intercept_loc: {}\nexpected: {}\nerror: {}".format(intercept_post, intercept_true, np.abs(intercept_post-intercept_true)))
    print("intercept_std: {}".format(np.exp(params['intercept_std_log'])))
    print("")

    X_test, y_test = test_data
    rng, rng_acc_true, rng_acc_post = jax.random.split(rng, 3)
    # for evaluation accuracy with true parameters, we scale them to the same
    #   scale as the found posterior. (gives better results than normalized
    #   parameters (probably due to numerical instabilities))
    acc_true = estimate_accuracy_fixed_params(X_test, y_test, w_true*scale_post, intercept_true*scale_post, rng_acc_true, 10)
    acc_post = estimate_accuracy(X_test, y_test, params, rng_acc_post, 10)

    print("avg accuracy on test set:  with true parameters: {} ; with found posterior: {}\n".format(acc_true, acc_post))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="parse args")
    parser.add_argument('-n', '--num-epochs', default=600, type=int, help='number of training epochs')
    parser.add_argument('-lr', '--learning-rate', default=1.0e-3, type=float, help='learning rate')
    parser.add_argument('-batch-size', default=200, type=int, help='batch size')
    parser.add_argument('-d', '--dimensions', default=4, type=int, help='data dimension')
    parser.add_argument('-N', '--num-samples', default=10000, type=int, help='data samples count')
    args = parser.parse_args()
    main(args)
