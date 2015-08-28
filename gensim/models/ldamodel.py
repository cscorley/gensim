#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2011 Radim Rehurek <radimrehurek@seznam.cz>
# Licensed under the GNU LGPL v2.1 - http://www.gnu.org/licenses/lgpl.html
#
# Parts of the LDA inference code come from Dr. Hoffman's `onlineldavb.py` script,
# (C) 2010  Matthew D. Hoffman, GNU GPL 3.0


"""
**For a faster implementation of LDA (parallelized for multicore machines), see** :mod:`gensim.models.ldamulticore`.

Latent Dirichlet Allocation (LDA) in Python.

This module allows both LDA model estimation from a training corpus and inference of topic
distribution on new, unseen documents. The model can also be updated with new documents
for online training.

The core estimation code is based on the `onlineldavb.py` script by M. Hoffman [1]_, see
**Hoffman, Blei, Bach: Online Learning for Latent Dirichlet Allocation, NIPS 2010.**

The algorithm:

* is **streamed**: training documents may come in sequentially, no random access required,
* runs in **constant memory** w.r.t. the number of documents: size of the
  training corpus does not affect memory footprint, can process corpora larger than RAM, and
* is **distributed**: makes use of a cluster of machines, if available, to
  speed up model estimation.

.. [1] http://www.cs.princeton.edu/~mdhoffma

"""


import logging
import sys
import numpy  # for arrays, array broadcasting etc.

from gensim import interfaces, utils, matutils
from itertools import chain
from scipy.special import gammaln, psi  # gamma function utils
from scipy.special import polygamma
from six.moves import xrange
import six

# log(sum(exp(x))) that tries to avoid overflow
try:
    # try importing from here if older scipy is installed
    from scipy.maxentropy import logsumexp
except ImportError:
    # maxentropy has been removed in recent releases, logsumexp now in misc
    from scipy.misc import logsumexp


logger = logging.getLogger('gensim.models.ldamodel')


def dirichlet_expectation(alpha):
    """
    For a vector `theta~Dir(alpha)`, compute `E[log(theta)]`.

    """
    if (len(alpha.shape) == 1):
        result = psi(alpha) - psi(numpy.sum(alpha))
    else:
        result = psi(alpha) - psi(numpy.sum(alpha, 1))[:, numpy.newaxis]
    return result.astype(alpha.dtype)  # keep the same precision as input


class LdaState(utils.SaveLoad):
    """
    Encapsulate information for distributed computation of LdaModel objects.

    Objects of this class are sent over the network, so try to keep them lean to
    reduce traffic.

    """
    def __init__(self, eta, shape):
        self.eta = eta
        self.sstats = numpy.zeros(shape)
        self.numdocs = 0

    def reset(self):
        """
        Prepare the state for a new EM iteration (reset sufficient stats).

        """
        self.sstats[:] = 0.0
        self.numdocs = 0

    def merge(self, other):
        """
        Merge the result of an E step from one node with that of another node
        (summing up sufficient statistics).

        The merging is trivial and after merging all cluster nodes, we have the
        exact same result as if the computation was run on a single node (no
        approximation).

        """
        assert other is not None
        self.sstats += other.sstats
        self.numdocs += other.numdocs

    def blend(self, rhot, other, targetsize=None):
        """
        Given LdaState `other`, merge it with the current state. Stretch both to
        `targetsize` documents before merging, so that they are of comparable
        magnitude.

        Merging is done by average weighting: in the extremes, `rhot=0.0` means
        `other` is completely ignored; `rhot=1.0` means `self` is completely ignored.

        This procedure corresponds to the stochastic gradient update from Hoffman
        et al., algorithm 2 (eq. 14).

        """
        assert other is not None
        if targetsize is None:
            targetsize = self.numdocs

        # stretch the current model's expected n*phi counts to target size
        if self.numdocs == 0 or targetsize == self.numdocs:
            scale = 1.0
        else:
            scale = 1.0 * targetsize / self.numdocs
        self.sstats *= (1.0 - rhot) * scale

        # stretch the incoming n*phi counts to target size
        if other.numdocs == 0 or targetsize == other.numdocs:
            scale = 1.0
        else:
            logger.info("merging changes from %i documents into a model of %i documents",
                        other.numdocs, targetsize)
            scale = 1.0 * targetsize / other.numdocs
        self.sstats += rhot * scale * other.sstats

        self.numdocs = targetsize

    def blend2(self, rhot, other, targetsize=None):
        """
        Alternative, more simple blend.
        """
        assert other is not None
        if targetsize is None:
            targetsize = self.numdocs

        # merge the two matrices by summing
        self.sstats += other.sstats
        self.numdocs = targetsize

    def get_lambda(self):
        return self.eta + self.sstats

    def get_Elogbeta(self):
        return dirichlet_expectation(self.get_lambda())
# endclass LdaState


class LdaModel(interfaces.TransformationABC):
    """
    The constructor estimates Latent Dirichlet Allocation model parameters based
    on a training corpus:

    >>> lda = LdaModel(corpus, num_topics=10)

    You can then infer topic distributions on new, unseen documents, with

    >>> doc_lda = lda[doc_bow]

    The model can be updated (trained) with new documents via

    >>> lda.update(other_corpus)

    Model persistency is achieved through its `load`/`save` methods.
    """
    def __init__(self, corpus=None, num_topics=100, id2word=None,
                 distributed=False, chunksize=None, passes=1, update_every=None,
                 alpha='symmetric', eta=None, decay=0.5, offset=1.0,
                 eval_every=None, iterations=50, gamma_threshold=0.001,
                 minimum_probability=0.01, algorithm=None,
                 max_bound_iterations=None, bound_improvement_threshold=0.001):
        """
        If given, start training from the iterable `corpus` straight away. If not given,
        the model is left untrained (presumably because you want to call `update()` manually).

        `num_topics` is the number of requested latent topics to be extracted from
        the training corpus.

        `id2word` is a mapping from word ids (integers) to words (strings). It is
        used to determine the vocabulary size, as well as for debugging and topic
        printing.

        `alpha` and `eta` are hyperparameters that affect sparsity of the document-topic
        (theta) and topic-word (lambda) distributions. Both default to a symmetric
        1.0/num_topics prior.

        `alpha` can be set to an explicit array = prior of your choice. It also
        support special values of 'asymmetric' and 'auto': the former uses a fixed
        normalized asymmetric 1.0/topicno prior, the latter learns an asymmetric
        prior directly from your data.

        `eta` can be a scalar for a symmetric prior over topic/word
        distributions, or a matrix of shape num_topics x num_words,
        which can be used to impose asymmetric priors over the word
        distribution on a per-topic basis. This may be useful if you
        want to seed certain topics with particular words by boosting
        the priors for those words. It also supports the special value 'auto'
        which learns an asymmetric prior directly from your data.

        Turn on `distributed` to force distributed computing (see the `web tutorial <http://radimrehurek.com/gensim/distributed.html>`_
        on how to set up a cluster of machines for gensim).

        Calculate and log perplexity estimate and per-word evidence lower bound (ELBO)
        from the latest mini-batch every
        `eval_every` model updates (setting this to 1 slows down training ~2x;
        default is 10 for better performance). Set to None to disable perplexity estimation.
        
        Setting `max_bound_iterations` to a suitably high number will allow
        iteration until the evidence lower bound no longer improves by more than
        `bound_improvement_threshold` times the previous iteration's lower bound.
        This requires perplexity estimation to be enabled.
        
        The algorithm will iteratively improve the topic distribution of each
        chunk of documents every pass or bound iteration
        until it changes by less than `gamma_threshold` or 
        `iterations` number of iterations is reached. The number of documents
        in a chunk is determined by `chunksize`.
        The word distributions of
        each topic are updated after every `update_every` chunks. Set 
        `update_every` to 0 to update the word distributions of each topic
        after the topic distributions have been updated for every document.

        `decay` and `offset` parameters are the same as Kappa and Tau_0 in
        Hoffman et al, respectively.

        `minimum_probability` controls filtering the topics returned for
        a document (bow).
        
        Setting `algorithm` to 'batch' will set up the
        relevant parameters above so that
        the algorithm used is the batch variational Bayes algorithm described
        in Hoffman et al. Similarly, setting `algorithm` to 'online' will set
        up the parameters above so that the algorithm used is the online 
        variational Bayes algorithm described in Hoffman et al.

        Example:

        >>> lda = LdaModel(corpus, num_topics=100)  # train model
        >>> print(lda[doc_bow]) # get topic probability distribution for a document
        >>> lda.update(corpus2) # update the LDA model with additional documents
        >>> print(lda[doc_bow])

        >>> lda = LdaModel(corpus, num_topics=50, alpha='auto', eval_every=5)  # train asymmetric alpha from data

        >>> lda = LdaModel(corpus, num_topics=50, alpha='auto', eta='auto', algorithm='batch')  # train asymmetric alpha and eta from data using batch algorithm

        """
        # store user-supplied parameters
        self.id2word = id2word
        if corpus is None and self.id2word is None:
            raise ValueError('at least one of corpus/id2word must be specified, to establish input space dimensionality')

        if self.id2word is None:
            logger.warning("no word id mapping provided; initializing from corpus, assuming identity")
            self.id2word = utils.dict_from_corpus(corpus)
            self.num_terms = len(self.id2word)
        elif len(self.id2word) > 0:
            self.num_terms = 1 + max(self.id2word.keys())
        else:
            self.num_terms = 0

        if self.num_terms == 0:
            raise ValueError("cannot compute LDA over an empty collection (no terms)")
        
        if algorithm == 'batch':
            if update_every is None:
                update_every = 0 # Default for batch algorithm
            elif update_every != 0:
                raise ValueError("The batch algorithm requires update_every = 0.")
            
            if eval_every is None:
                eval_every = 1 # Default for batch algorithm
            elif eval_every == 0:
                raise ValueError("The batch algorithm requires eval_every > 0.")

            if chunksize is None:
                chunksize = sys.maxint # Default for batch algorithm
            elif chunksize < sys.maxint:
                raise ValueError("The batch algorithm does not use multiple chunks.")
                
            if passes is None:
                passes = 1
            elif passes > 1:
                raise ValueError("The batch algorithm does not use multiple passes.")

            if max_bound_iterations is None:
                max_bound_iterations = 1000
            elif max_bound_iterations <= 1:
                raise ValueError("The batch algorithm uses multiple bound iterations.")

        elif algorithm == 'online':
            if update_every is None:
                update_every = 1 # Default for online algorithm
            elif update_every <= 0:
                raise ValueError("The online algorithm requires update_every > 0.")
            
            if max_bound_iterations is None:
                max_bound_iterations = 1
            elif max_bound_iterations != 1:
                raise ValueError("The online algorithm does not use multiple bound iterations.")
            
        elif algorithm != None:
            raise ValueError("Unknown algorithm specified.")

        if update_every is None:
            update_every = 1 # Default for online algorithm
        if eval_every is None:
            eval_every = 10 # Default for online algorithm
        if chunksize is None:
            chunksize = 2000 # Default for online algorithm
        if max_bound_iterations is None:
            max_bound_iterations = 1
            

        self.distributed = bool(distributed)
        self.num_topics = int(num_topics)
        self.chunksize = chunksize
        self.decay = decay
        self.offset = offset
        self.minimum_probability = minimum_probability
        self.num_updates = 0

        self.passes = passes
        self.update_every = update_every
        self.eval_every = eval_every
        
        self.max_bound_iterations = max_bound_iterations
        self.bound_improvement_threshold = bound_improvement_threshold

        self.optimize_alpha = alpha == 'auto'
        if alpha == 'symmetric' or alpha is None:
            logger.info("using symmetric alpha at %s", 1.0 / num_topics)
            self.alpha = numpy.asarray([1.0 / num_topics for i in xrange(num_topics)])
        elif alpha == 'asymmetric':
            self.alpha = numpy.asarray([1.0 / (i + numpy.sqrt(num_topics)) for i in xrange(num_topics)])
            self.alpha /= self.alpha.sum()
            logger.info("using asymmetric alpha %s", list(self.alpha))
        elif alpha == 'auto':
            self.alpha = numpy.asarray([1.0 / num_topics for i in xrange(num_topics)])
            logger.info("using autotuned alpha, starting with %s", list(self.alpha))
        else:
            # must be either float or an array of floats, of size num_topics
            self.alpha = alpha if isinstance(alpha, numpy.ndarray) else numpy.asarray([alpha] * num_topics)
            if len(self.alpha) != num_topics:
                raise RuntimeError("invalid alpha shape (must match num_topics)")

        self.optimize_eta = eta == 'auto'
        if eta is None:
            self.eta = 1.0 / num_topics 
        elif eta == 'auto':
            # this needs to be a column vector of length num_topics
            self.eta = numpy.asarray([1.0 / num_topics for i in xrange(num_topics)]).reshape((num_topics,1))
            logger.info("using autotuned eta, starting with %s", list(self.eta))
        else:
            self.eta = eta

        # VB constants
        self.iterations = iterations
        self.gamma_threshold = gamma_threshold

        # set up distributed environment if necessary
        if not distributed:
            logger.info("using serial LDA version on this node")
            self.dispatcher = None
            self.numworkers = 1
        else:
            if self.optimize_alpha:
                raise NotImplementedError("auto-optimizing alpha not implemented in distributed LDA")
            # set up distributed version
            try:
                import Pyro4
                dispatcher = Pyro4.Proxy('PYRONAME:gensim.lda_dispatcher')
                logger.debug("looking for dispatcher at %s" % str(dispatcher._pyroUri))
                dispatcher.initialize(id2word=self.id2word, num_topics=num_topics,
                                      chunksize=chunksize, alpha=alpha, eta=eta, distributed=False)
                self.dispatcher = dispatcher
                self.numworkers = len(dispatcher.getworkers())
                logger.info("using distributed version with %i workers" % self.numworkers)
            except Exception as err:
                logger.error("failed to initialize distributed LDA (%s)", err)
                raise RuntimeError("failed to initialize distributed LDA (%s)" % err)

        # Initialize the variational distribution q(beta|lambda)
        self.state = LdaState(self.eta, (self.num_topics, self.num_terms))
        self.state.sstats = numpy.random.gamma(100., 1. / 100., (self.num_topics, self.num_terms))
        self.sync_state()

        # if a training corpus was provided, start estimating the model right away
        if corpus is not None:
            self.update(corpus)

    def __str__(self):
        return "LdaModel(num_terms=%s, num_topics=%s, decay=%s, chunksize=%s)" % \
            (self.num_terms, self.num_topics, self.decay, self.chunksize)

    def sync_state(self):
        self.expElogbeta = numpy.exp(self.state.get_Elogbeta())

    def clear(self):
        """Clear model state (free up some memory). Used in the distributed algo."""
        self.state = None
        self.Elogbeta = None

    def inference(self, chunk, collect_sstats=False):
        """
        Given a chunk of sparse document vectors, estimate gamma (parameters
        controlling the topic weights) for each document in the chunk.

        This function does not modify the model (=is read-only aka const). The
        whole input chunk of document is assumed to fit in RAM; chunking of a
        large corpus must be done earlier in the pipeline.

        If `collect_sstats` is True, also collect sufficient statistics needed
        to update the model's topic-word distributions, and return a 2-tuple
        `(gamma, sstats)`. Otherwise, return `(gamma, None)`. `gamma` is of shape
        `len(chunk) x self.num_topics`.

        Avoids computing the `phi` variational parameter directly using the
        optimization presented in **Lee, Seung: Algorithms for non-negative matrix factorization, NIPS 2001**.

        """
        try:
            _ = len(chunk)
        except:
            # convert iterators/generators to plain list, so we have len() etc.
            chunk = list(chunk)
        if len(chunk) > 1:
            logger.debug("performing inference on a chunk of %i documents", len(chunk))

        # Initialize the variational distribution q(theta|gamma) for the chunk
        gamma = numpy.random.gamma(100., 1. / 100., (len(chunk), self.num_topics))
        Elogtheta = dirichlet_expectation(gamma)
        expElogtheta = numpy.exp(Elogtheta)
        if collect_sstats:
            sstats = numpy.zeros_like(self.expElogbeta)
        else:
            sstats = None
        converged = 0

        # Now, for each document d update that document's gamma and phi
        # Inference code copied from Hoffman's `onlineldavb.py` (esp. the
        # Lee&Seung trick which speeds things up by an order of magnitude, compared
        # to Blei's original LDA-C code, cool!).
        for d, doc in enumerate(chunk):
            ids = [id for id, _ in doc]
            cts = numpy.array([cnt for _, cnt in doc])
            gammad = gamma[d, :]
            Elogthetad = Elogtheta[d, :]
            expElogthetad = expElogtheta[d, :]
            expElogbetad = self.expElogbeta[:, ids]

            # The optimal phi_{dwk} is proportional to expElogthetad_k * expElogbetad_w.
            # phinorm is the normalizer.
            # TODO treat zeros explicitly, instead of adding 1e-100?
            phinorm = numpy.dot(expElogthetad, expElogbetad) + 1e-100

            # Iterate between gamma and phi until convergence
            for _ in xrange(self.iterations):
                lastgamma = gammad
                # We represent phi implicitly to save memory and time.
                # Substituting the value of the optimal phi back into
                # the update for gamma gives this update. Cf. Lee&Seung 2001.
                gammad = self.alpha + expElogthetad * numpy.dot(cts / phinorm, expElogbetad.T)
                Elogthetad = dirichlet_expectation(gammad)
                expElogthetad = numpy.exp(Elogthetad)
                phinorm = numpy.dot(expElogthetad, expElogbetad) + 1e-100
                # If gamma hasn't changed much, we're done.
                meanchange = numpy.mean(abs(gammad - lastgamma))
                if (meanchange < self.gamma_threshold):
                    converged += 1
                    break
            gamma[d, :] = gammad
            if collect_sstats:
                # Contribution of document d to the expected sufficient
                # statistics for the M step.
                sstats[:, ids] += numpy.outer(expElogthetad.T, cts / phinorm)

        if len(chunk) > 1:
            logger.debug("%i/%i documents converged within %i iterations",
                         converged, len(chunk), self.iterations)

        if collect_sstats:
            # This step finishes computing the sufficient statistics for the
            # M step, so that
            # sstats[k, w] = \sum_d n_{dw} * phi_{dwk}
            # = \sum_d n_{dw} * exp{Elogtheta_{dk} + Elogbeta_{kw}} / phinorm_{dw}.
            sstats *= self.expElogbeta
        return gamma, sstats

    def do_estep(self, chunk, state=None):
        """
        Perform inference on a chunk of documents, and accumulate the collected
        sufficient statistics in `state` (or `self.state` if None).

        """
        if state is None:
            state = self.state
        gamma, sstats = self.inference(chunk, collect_sstats=True)
        state.sstats += sstats
        state.numdocs += gamma.shape[0]  # avoids calling len(chunk) on a generator
        return gamma

    def update_alpha(self, gammat, rho):
        """
        Update parameters for the Dirichlet prior on the per-document
        topic weights `alpha` given the last `gammat`.

        Uses Newton's method, described in **Huang: Maximum Likelihood Estimation of Dirichlet Distribution Parameters.**
        http://jonathan-huang.org/research/dirichlet/dirichlet.pdf

        """
        N = float(len(gammat))
        logphat = sum(dirichlet_expectation(gamma) for gamma in gammat) / N
        dalpha = numpy.copy(self.alpha)
        gradf = N * (psi(numpy.sum(self.alpha)) - psi(self.alpha) + logphat)

        c = N * polygamma(1, numpy.sum(self.alpha))
        q = -N * polygamma(1, self.alpha)

        b = numpy.sum(gradf / q) / (1 / c + numpy.sum(1 / q))

        dalpha = -(gradf - b) / q

        if all(rho * dalpha + self.alpha > 0):
            self.alpha += rho * dalpha
        else:
            logger.warning("updated alpha not positive")
        logger.info("optimized alpha %s", list(self.alpha))

        return self.alpha

    def update_eta(self, lambdat, rho):
        """
        Update parameters for the Dirichlet prior on the per-topic
        word weights `eta` given the last `lambdat`.

        Uses Newton's method, described in **Huang: Maximum Likelihood Estimation of Dirichlet Distribution Parameters.**
        http://jonathan-huang.org/research/dirichlet/dirichlet.pdf

        """
        if self.eta.shape[1] != 1:
            raise ValueError("Can't use update_eta with eta matrices, only colunmn vectors.")
        N = float(lambdat.shape[1])
        logphat = (sum(dirichlet_expectation(lambda_) for lambda_ in lambdat.transpose()) / N).reshape((self.num_topics,1))
        deta = numpy.copy(self.eta)
        gradf = N * (psi(numpy.sum(self.eta)) - psi(self.eta) + logphat)

        c = N * polygamma(1, numpy.sum(self.eta))
        q = -N * polygamma(1, self.eta)

        b = numpy.sum(gradf / q) / (1 / c + numpy.sum(1 / q))

        deta = -(gradf - b) / q
        #logger.info(repr(gradf.shape) + " " + repr(logphat.shape))
        #logger.info(list(deta.reshape((10))))
        if all(rho * deta + self.eta > 0):
            self.eta += rho * deta
        else:
            logger.warning("updated eta not positive")
        logger.info("optimized eta %s", list(self.eta.reshape((self.num_topics))))

        return self.eta

    def log_perplexity(self, chunk, total_docs=None):
        """
        Calculate and return per-word likelihood bound, using the `chunk` of
        documents as evaluation corpus. Also output the calculated statistics. incl.
        perplexity=2^(-bound), to log at INFO level.

        """
        if total_docs is None:
            total_docs = len(chunk)
        corpus_words = sum(cnt for document in chunk for _, cnt in document)
        subsample_ratio = 1.0 * total_docs / len(chunk)
        bound = self.bound(chunk, subsample_ratio=subsample_ratio)
        perwordbound = bound / (subsample_ratio * corpus_words)
        logger.info("%.3f per-word bound, %.1f perplexity estimate based on a held-out corpus of %i documents with %i words" %
                    (perwordbound, numpy.exp2(-perwordbound), len(chunk), corpus_words))
        return perwordbound

    def update(self, corpus, chunksize=None, decay=None, offset=None,
               passes=None, update_every=None, eval_every=None, iterations=None,
               gamma_threshold=None, max_bound_iterations=None, bound_improvement_threshold=None):
        """
        Train the model with new documents, by EM-iterating over `corpus` until
        the topics converge (or until the maximum number of allowed iterations
        is reached). `corpus` must be an iterable (repeatable stream of documents),

        In distributed mode, the E step is distributed over a cluster of machines.

        This update also supports updating an already trained model (`self`)
        with new documents from `corpus`; the two models are then merged in
        proportion to the number of old vs. new documents. This feature is still
        experimental for non-stationary input streams.

        For stationary input (no topic drift in new documents), on the other hand,
        this equals the online update of Hoffman et al. and is guaranteed to
        converge for any `decay` in (0.5, 1.0>. Additionally, for smaller
        `corpus` sizes, an increasing `offset` may be beneficial (see
        Table 1 in Hoffman et al.)
        
        Optionally, iteration until the evidence lower bound (ELBO) converges
        (no significant improvements to topic allocations can be made)
        can be obtained by specifying a suitably high `max_bound_iterations`
        when using the batch update type. Batch updates can be enabled by
        specifying `update_every=0`.

        """
        # use parameters given in constructor, unless user explicitly overrode them
        if decay is None:
            decay = self.decay
        if offset is None:
            offset = self.offset
        if passes is None:
            passes = self.passes
        if update_every is None:
            update_every = self.update_every
        if eval_every is None:
            eval_every = self.eval_every
        if iterations is None:
            iterations = self.iterations
        if gamma_threshold is None:
            gamma_threshold = self.gamma_threshold
        if max_bound_iterations is None:
            max_bound_iterations = self.max_bound_iterations
        if bound_improvement_threshold is None:
            bound_improvement_threshold = self.bound_improvement_threshold

        try:
            lencorpus = len(corpus)
        except:
            logger.warning("input corpus stream has no len(); counting documents")
            lencorpus = sum(1 for _ in corpus)
        if lencorpus == 0:
            logger.warning("LdaModel.update() called with an empty corpus")
            return

        if chunksize is None:
            chunksize = min(lencorpus, self.chunksize)

        self.state.numdocs += lencorpus

        if update_every:
            updatetype = "online"
            updateafter = min(lencorpus, update_every * self.numworkers * chunksize)
            if max_bound_iterations > 1:
                raise ValueError("It doesn't make sense to use "
                                 "max_bound_iterations in online mode.")
        else:
            updatetype = "batch"
            updateafter = lencorpus
        evalafter = min(lencorpus, (eval_every or 0) * self.numworkers * chunksize)
        
        if max_bound_iterations < 1:
            raise ValueError("max_bound_iterations must be at least 1.")
        
        if max_bound_iterations > 1 and not (eval_every or 0) > 0:
            raise ValueError("eval_every must be set (usually to 1) "
                             "for max_bound_iterations > 1")
        
        if max_bound_iterations > 1 and chunksize < lencorpus:
            logger.warning("Using multiple chunks with max_bound_iterations > 1 "
                           "isn't proven to converge.")

        if max_bound_iterations > 1 and passes > 1:
            logger.warning("Using multiple passes with max_bound_iterations > 1 "
                           "is probably useless, decrease "
                           "bound_improvement_threshold instead.")

        updates_per_pass = max(1, lencorpus / updateafter)
        if max_bound_iterations > 1:
            logger.info("running %s LDA training, %s topics, using "
                        "the supplied corpus of %i documents, updating model once "
                        "every %i documents, evaluating perplexity every %i documents, "
                        "iterating %ix with a convergence threshold of %f "
                        "until the bound improves by less than %f times the previous bound "
                        "or the corpus has been passed over %i times.",
                        updatetype, self.num_topics, lencorpus,
                            updateafter, evalafter, iterations,
                            gamma_threshold, bound_improvement_threshold,
                            passes*max_bound_iterations)
        else:
            logger.info("running %s LDA training, %s topics, %i passes over "
                        "the supplied corpus of %i documents, updating model once "
                        "every %i documents, evaluating perplexity every %i documents, "
                        "iterating %ix with a convergence threshold of %f",
                        updatetype, self.num_topics, passes, lencorpus,
                            updateafter, evalafter, iterations,
                            gamma_threshold)

        if updates_per_pass * passes < 10 and max_bound_iterations == 1:
            logger.warning("too few updates, training might not converge; consider "
                           "increasing the number of passes or iterations to improve accuracy")

        # rho is the "speed" of updating; TODO try other fncs
        # pass_ + num_updates handles increasing the starting t for each pass,
        # while allowing it to "reset" on the first pass of each update
        def rho():
            return pow(offset + pass_ + (self.num_updates / chunksize), -decay)
        
        for pass_ in xrange(passes):
            num_updates_at_pass_start = self.num_updates
            last_perwordbound = 1e99
            for bound_iteration in xrange(max_bound_iterations):
                if bound_iteration > 0:
                    # reset num_updates so that rho is the same for each bound_iteration
                    self.num_updates = num_updates_at_pass_start
                
                if self.dispatcher:
                    logger.info('initializing %s workers' % self.numworkers)
                    self.dispatcher.reset(self.state)
                else:
                    other = LdaState(self.eta, self.state.sstats.shape)
                dirty = False

                reallen = 0
                perwordbound_updated = False
                for chunk_no, chunk in enumerate(utils.grouper(corpus, chunksize, as_numpy=True)):
                    reallen += len(chunk)  # keep track of how many documents we've processed so far

                    if eval_every and ((reallen == lencorpus) or ((chunk_no + 1) % (eval_every * self.numworkers) == 0)):
                        perwordbound = self.log_perplexity(chunk, total_docs=lencorpus)
                        perwordbound_updated = True
                        # keep track of the total bound for bound_iterations

                    if self.dispatcher:
                        # add the chunk to dispatcher's job queue, so workers can munch on it
                        logger.info('PROGRESS: pass %i, dispatching documents up to #%i/%i',
                                    pass_, chunk_no * chunksize + len(chunk), lencorpus)
                        # this will eventually block until some jobs finish, because the queue has a small finite length
                        self.dispatcher.putjob(chunk)
                    else:
                        logger.info('PROGRESS: pass %i, at document #%i/%i',
                                    pass_, chunk_no * chunksize + len(chunk), lencorpus)
                        gammat = self.do_estep(chunk, other)

                        if self.optimize_alpha:
                            self.update_alpha(gammat, rho())

                    dirty = True
                    del chunk

                    # perform an M step. determine when based on update_every, don't do this after every chunk
                    if update_every and (chunk_no + 1) % (update_every * self.numworkers) == 0:
                        if self.dispatcher:
                            # distributed mode: wait for all workers to finish
                            logger.info("reached the end of input; now waiting for all remaining jobs to finish")
                            other = self.dispatcher.getstate()
                        self.do_mstep(rho(), other, pass_ > 0)
                        del other  # frees up memory

                        if self.dispatcher:
                            logger.info('initializing workers')
                            self.dispatcher.reset(self.state)
                        else:
                            other = LdaState(self.eta, self.state.sstats.shape)
                        dirty = False
                # endfor single corpus iteration
                if reallen != lencorpus:
                    raise RuntimeError("input corpus size changed during training (don't use generators as input)")

                if dirty:
                    # finish any remaining updates
                    if self.dispatcher:
                        # distributed mode: wait for all workers to finish
                        logger.info("reached the end of input; now waiting for all remaining jobs to finish")
                        other = self.dispatcher.getstate()
                    self.do_mstep(rho(), other, pass_ > 0)
                    del other
                    dirty = False
                
                if perwordbound_updated: # if eval_every > 1 improvement may be exactly 0 because 
                                         # the bound wasnt recomputed
                                         # it would be incorrect to terminate bound_iterations in that case
                    relative_improvement = (last_perwordbound-perwordbound)/last_perwordbound
                    logger.info("EM Iteration %i: %.3f per-word bound, %.6f improvement" %
                                (bound_iteration, perwordbound, relative_improvement))
                    if relative_improvement < bound_improvement_threshold:
                        break
                    last_perwordbound = perwordbound
            # endfor bound_iteration
        # endfor entire corpus update


    def do_mstep(self, rho, other, extra_pass=False):
        """
        M step: use linear interpolation between the existing topics and
        collected sufficient statistics in `other` to update the topics.

        """
        logger.debug("updating topics")
        # update self with the new blend; also keep track of how much did
        # the topics change through this update, to assess convergence
        diff = numpy.log(self.expElogbeta)
        self.state.blend(rho, other)
        diff -= self.state.get_Elogbeta()
        self.sync_state()

        # print out some debug info at the end of each EM iteration
        self.print_topics(5)
        diffnorm = numpy.mean(numpy.abs(diff))
        logger.info("topic diff=%f, rho=%f", numpy.mean(numpy.abs(diff)), rho)
        
        if self.optimize_eta:
            self.update_eta(self.state.get_lambda(), rho)

        if not extra_pass:
            # only update if this isn't an additional pass
            self.num_updates += other.numdocs

    def bound(self, corpus, gamma=None, subsample_ratio=1.0):
        """
        Estimate the variational bound of documents from `corpus`:
        E_q[log p(corpus)] - E_q[log q(corpus)]

        `gamma` are the variational parameters on topic weights for each `corpus`
        document (=2d matrix=what comes out of `inference()`).
        If not supplied, will be inferred from the model.

        """
        score = 0.0
        _lambda = self.state.get_lambda()
        Elogbeta = dirichlet_expectation(_lambda)

        for d, doc in enumerate(corpus):  # stream the input doc-by-doc, in case it's too large to fit in RAM
            if d % self.chunksize == 0:
                logger.debug("bound: at document #%i", d)
            if gamma is None:
                gammad, _ = self.inference([doc])
            else:
                gammad = gamma[d]
            Elogthetad = dirichlet_expectation(gammad)

            # E[log p(doc | theta, beta)]
            score += numpy.sum(cnt * logsumexp(Elogthetad + Elogbeta[:, id]) for id, cnt in doc)

            # E[log p(theta | alpha) - log q(theta | gamma)]; assumes alpha is a vector
            score += numpy.sum((self.alpha - gammad) * Elogthetad)
            score += numpy.sum(gammaln(gammad) - gammaln(self.alpha))
            score += gammaln(numpy.sum(self.alpha)) - gammaln(numpy.sum(gammad))

        # compensate likelihood for when `corpus` above is only a sample of the whole corpus
        score *= subsample_ratio

        # E[log p(beta | eta) - log q (beta | lambda)]; assumes eta is a scalar
        score += numpy.sum((self.eta - _lambda) * Elogbeta)
        score += numpy.sum(gammaln(_lambda) - gammaln(self.eta))

        if numpy.ndim(self.eta) == 0:
            sum_eta = self.eta * self.num_terms
        else:
            sum_eta = numpy.sum(self.eta, 1)

        score += numpy.sum(gammaln(sum_eta) - gammaln(numpy.sum(_lambda, 1)))
        return score

    def print_topics(self, num_topics=10, num_words=10):
        return self.show_topics(num_topics, num_words, log=True)

    def show_topics(self, num_topics=10, num_words=10, log=False, formatted=True):
        """
        For `num_topics` number of topics, return `num_words` most significant words
        (10 words per topic, by default).

        The topics are returned as a list -- a list of strings if `formatted` is
        True, or a list of (probability, word) 2-tuples if False.

        If `log` is True, also output this result to log.

        Unlike LSA, there is no natural ordering between the topics in LDA.
        The returned `num_topics <= self.num_topics` subset of all topics is therefore
        arbitrary and may change between two LDA training runs.

        """
        if num_topics < 0 or num_topics >= self.num_topics:
            num_topics = self.num_topics
            chosen_topics = range(num_topics)
        else:
            num_topics = min(num_topics, self.num_topics)

            # add a little random jitter, to randomize results around the same alpha
            sort_alpha = self.alpha + 0.0001 * numpy.random.rand(len(self.alpha))

            sorted_topics = list(matutils.argsort(sort_alpha))
            chosen_topics = sorted_topics[:num_topics // 2] + sorted_topics[-num_topics // 2:]

        shown = []
        for i in chosen_topics:
            if formatted:
                topic = self.print_topic(i, topn=num_words)
            else:
                topic = self.show_topic(i, topn=num_words)

            shown.append(topic)
            if log:
                logger.info("topic #%i (%.3f): %s", i, self.alpha[i], topic)

        return shown

    def show_topic(self, topicid, topn=10):
        """
        Return a list of `(words_probability, word)` 2-tuples for the most probable
        words in topic `topicid`.

        Only return 2-tuples for the topn most probable words (ignore the rest).

        """
        topic = self.state.get_lambda()[topicid]
        topic = topic / topic.sum()  # normalize to probability distribution
        bestn = matutils.argsort(topic, topn, reverse=True)
        beststr = [(topic[id], self.id2word[id]) for id in bestn]
        return beststr

    def print_topic(self, topicid, topn=10):
        """Return the result of `show_topic`, but formatted as a single string."""
        return ' + '.join(['%.3f*%s' % v for v in self.show_topic(topicid, topn)])

    def top_topics(self, corpus, num_words=20):
        """
        Calculate the Umass topic coherence for each topic. Algorithm from
        **Mimno, Wallach, Talley, Leenders, McCallum: Optimizing Semantic Coherence in Topic Models, CEMNLP 2011.**
        """
        is_corpus, corpus = utils.is_corpus(corpus)
        if not is_corpus:
            logger.warning("LdaModel.top_topics() called with an empty corpus")
            return

        topics = []
        str_topics = []
        for topic in self.state.get_lambda():
            topic = topic / topic.sum()  # normalize to probability distribution
            bestn = matutils.argsort(topic, topn=num_words, reverse=True)
            topics.append(bestn)
            beststr = [(topic[id], self.id2word[id]) for id in bestn]
            str_topics.append(beststr)

        # top_ids are limited to every topics top words. should not exceed the
        # vocabulary size.
        top_ids = set(chain.from_iterable(topics))

        # create a document occurence sparse matrix for each word
        doc_word_list = {}
        for id in top_ids:
            id_list = set()
            for n, document in enumerate(corpus):
                if id in frozenset(x[0] for x in document):
                    id_list.add(n)

            doc_word_list[id] = id_list

        coherence_scores = []
        for t, top_words in enumerate(topics):
            # Calculate each coherence score C(t, top_words)
            coherence = 0.0
            # Sum of top words m=2..M
            for m in top_words[1:]:
                # m_docs is v_m^(t)
                m_docs = doc_word_list[m]

                # Sum of top words l=1..m-1
                # i.e., all words ranked higher than the current word m
                for l in top_words[:m - 1]:
                    # l_docs is v_l^(t)
                    l_docs = doc_word_list[l]

                    # co_doc_frequency is D(v_m^(t), v_l^(t))
                    co_doc_frequency = len(m_docs.intersection(l_docs))

                    # add to the coherence sum for these two words m, l
                    coherence += numpy.log((co_doc_frequency + 1.0) / len(l_docs))

            coherence_scores.append((str_topics[t], coherence))

        top_topics = sorted(coherence_scores, key=lambda t: t[1], reverse=True)
        return top_topics

    def get_document_topics(self, bow, minimum_probability=None):
        """
        Return topic distribution for the given document `bow`, as a list of
        (topic_id, topic_probability) 2-tuples.

        Ignore topics with very low probability (below `minimum_probability`).

        """
        if minimum_probability is None:
            minimum_probability = self.minimum_probability

        # if the input vector is a corpus, return a transformed corpus
        is_corpus, corpus = utils.is_corpus(bow)
        if is_corpus:
            return self._apply(corpus)

        gamma, _ = self.inference([bow])
        topic_dist = gamma[0] / sum(gamma[0])  # normalize distribution
        return [(topicid, topicvalue) for topicid, topicvalue in enumerate(topic_dist)
                if topicvalue >= minimum_probability]

    def __getitem__(self, bow, eps=None):
        """
        Return topic distribution for the given document `bow`, as a list of
        (topic_id, topic_probability) 2-tuples.

        Ignore topics with very low probability (below `eps`).

        """
        return self.get_document_topics(bow, eps)

    def save(self, fname, ignore=['state', 'dispatcher'], *args, **kwargs):
        """
        Save the model to file.

        Large internal arrays may be stored into separate files, with `fname` as prefix.
        
        `separately` can be used to define which arrays should be stored in separate files.
        
        `ignore` parameter can be used to define which variables should be ignored, i.e. left
        out from the pickled lda model. By default the internal `state` is ignored as it uses
        its own serialisation not the one provided by `LdaModel`. The `state` and `dispatcher
        will be added to any ignore parameter defined.


        Note: do not save as a compressed file if you intend to load the file back with `mmap`.

        Note: If you intend to use models across Python 2/3 versions there are a few things to
        keep in mind:

          1. The pickled Python dictionaries will not work across Python versions
          2. The `save` method does not automatically save all NumPy arrays using NumPy, only
             those ones that exceed `sep_limit` set in `gensim.utils.SaveLoad.save`. The main
             concern here is the `alpha` array if for instance using `alpha='auto'`.

        Please refer to the wiki recipes section (https://github.com/piskvorky/gensim/wiki/Recipes-&-FAQ#q9-how-do-i-load-a-model-in-python-3-that-was-trained-and-saved-using-python-2)
        for an example on how to work around these issues.
        """
        if self.state is not None:
            self.state.save(utils.smart_extension(fname, '.state'), *args, **kwargs)
        
        # make sure 'state' and 'dispatcher' are ignored from the pickled object, even if
        # someone sets the ignore list themselves
        if ignore is not None and ignore:
            if isinstance(ignore, six.string_types):
                ignore = [ignore]
            ignore = [e for e in ignore if e] # make sure None and '' are not in the list
            ignore = list(set(['state', 'dispatcher']) | set(ignore))
        else:
            ignore = ['state', 'dispatcher']
        super(LdaModel, self).save(fname, *args, ignore=ignore, **kwargs)

    @classmethod
    def load(cls, fname, *args, **kwargs):
        """
        Load a previously saved object from file (also see `save`).

        Large arrays can be memmap'ed back as read-only (shared memory) by setting `mmap='r'`:

            >>> LdaModel.load(fname, mmap='r')

        """
        kwargs['mmap'] = kwargs.get('mmap', None)
        result = super(LdaModel, cls).load(fname, *args, **kwargs)
        state_fname = utils.smart_extension(fname, '.state')
        try:
            result.state = super(LdaModel, cls).load(state_fname, *args, **kwargs)
        except Exception as e:
            logging.warning("failed to load state from %s: %s", state_fname, e)
        return result
# endclass LdaModel
