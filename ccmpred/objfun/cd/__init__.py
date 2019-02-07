import numpy as np
import ccmpred.raw
import ccmpred.gaps
import ccmpred.counts
import ccmpred.objfun
import ccmpred.objfun.cd.cext
import ccmpred.parameter_handling
from ccmpred.pseudocounts import PseudoCounts
import ccmpred.sampling

class ContrastiveDivergence():

    def __init__(self, msa, weights, regularization, pseudocounts, x_single, x_pair,
                 gibbs_steps=1, nr_seq_sample=500, persistent=False):


        self.msa = msa
        self.nrow, self.ncol = self.msa.shape
        self.weights = weights
        self.neff = np.sum(weights)
        self.regularization = regularization

        self.pseudocount_type       = pseudocounts.pseudocount_type
        self.pseudocount_n_single   = pseudocounts.pseudocount_n_single
        self.pseudocount_n_pair     = pseudocounts.pseudocount_n_pair

        self.structured_to_linear = lambda x_single, x_pair: \
            ccmpred.parameter_handling.structured_to_linear(
                x_single, x_pair, nogapstate=False, padding=False)
        self.linear_to_structured = lambda x: \
            ccmpred.parameter_handling.linear_to_structured(
                x, self.ncol, nogapstate=False, add_gap_state=False, padding=False)


        self.x_single = x_single
        self.x_pair = x_pair
        self.x = self.structured_to_linear(self.x_single, self.x_pair)

        self.nsingle = self.ncol * 21
        self.npair = self.ncol * self.ncol * 21 * 21
        self.nvar = self.nsingle + self.npair

        # get constant alignment counts - INCLUDING PSEUDO COUNTS
        # important for small alignments
        self.freqs_single, self.freqs_pair = pseudocounts.freqs
        self.msa_counts_single = self.freqs_single * self.neff
        self.msa_counts_pair = self.freqs_pair * self.neff

        # reset gap counts
        #self.msa_counts_single[:, 20] = 0
        #self.msa_counts_pair[:, :, :, 20] = 0
        #self.msa_counts_pair[:, :, 20, :] = 0

        # non_gapped counts
        self.Ni = self.msa_counts_single.sum(1)
        self.Nij = self.msa_counts_pair.sum(3).sum(2)

        ### Setting for (Persistent) Contrastive Divergence

        #perform this many steps of Gibbs sampling per sequence
        # 1 Gibbs step == sample every sequence position once
        self.gibbs_steps = np.max([gibbs_steps, 1])

        #define how many markov chains are run in parallel
        # => how many sequences are sampled at each iteration
        # at least 500 sequences or 10% of sequences in MSA
        self.nr_seq_sample = np.max([int(self.nrow/10), nr_seq_sample])

        #prepare the persistent MSA (Markov chains are NOT reset after each iteration)
        self.persistent=persistent
        #ensure that msa has at least NR_SEQ_SAMPLE sequences
        seq_id = list(range(self.nrow)) * int(np.ceil(self.nr_seq_sample / float(self.nrow)))
        self.msa_persistent = self.msa[seq_id]
        self.weights_persistent = self.weights[seq_id]

    def __repr__(self):

        repr_str = ""

        if self.persistent:
            repr_str += "persistent "

        repr_str += "contrastive divergence: "

        repr_str += "\nnr of sampled sequences={0} ({1}xN and {2}xNeff and {3}xL) Gibbs steps={4} ".format(
            self.nr_seq_sample,
            np.round(self.nr_seq_sample / float(self.nrow), decimals=3),
            np.round(self.nr_seq_sample / self.neff, decimals=3),
            np.round(self.nr_seq_sample / float(self.ncol), decimals=3),
            self.gibbs_steps
        )

        return repr_str

    def init_sample_alignment(self, persistent=False):
        """
        in case of CD:
            Randomly choose NR_SEQ_SAMPLE sequences from the ORIGINAL alignment
        in case of persistent CD:
            Randomly choose NR_SEQ_SAMPLE sequences from the alignment containing previously sampled sequences
        use the sequence weights computed from the original alignment
            (recomputing sequence weights in each iteration is too expensive)

        :return:
        """

        if persistent:
            # in case of PERSISTENT CD, continue the Markov chain:
            #randomly select NR_SEQ_SAMPLE sequences from persistent MSA
            self.sample_seq_id = np.random.choice(self.msa_persistent.shape[0], self.nr_seq_sample, replace=False)
            msa = self.msa_persistent[self.sample_seq_id]
            weights = self.weights_persistent[self.sample_seq_id]
        else:
            # in case of plain CD, reinitialize the Markov chains from original sequences:
            # randomly select NR_SEQ_SAMPLE sequences from original MSA
            self.sample_seq_id = np.random.choice(self.nrow, self.nr_seq_sample, replace=True)
            msa = self.msa[self.sample_seq_id]
            weights = self.weights[self.sample_seq_id]

        return msa, weights

    def finalize(self, x):
        return ccmpred.parameter_handling.linear_to_structured(
            x, self.ncol, clip=False, nogapstate=False, add_gap_state=False, padding=False
        )

    def evaluate(self, x, persistent=False):

        #setup sequences for sampling
        self.msa_sampled, self.msa_sampled_weights = self.init_sample_alignment(persistent)

        #Gibbs Sampling of sequences (each position of each sequence will be sampled this often: GIBBS_STEPS)
        self.msa_sampled = ccmpred.sampling.gibbs_sample_sequences(x, self.msa_sampled, self.gibbs_steps)

        if persistent:
            self.msa_persistent[self.sample_seq_id] = self.msa_sampled

        # compute amino acid frequencies from sampled alignment
        # add pseudocounts for stability
        pseudocounts = PseudoCounts(self.msa_sampled, self.msa_sampled_weights)
        pseudocounts.calculate_frequencies(
                self.pseudocount_type,
                self.pseudocount_n_single,
                self.pseudocount_n_pair,
                remove_gaps=False)

        #compute frequencies excluding gap counts
        #sampled_freq_single = pseudocounts.degap(pseudocounts.freqs[0], True)
        #sampled_freq_pair   = pseudocounts.degap(pseudocounts.freqs[1], True)
        sampled_freq_single = pseudocounts.freqs[0]
        sampled_freq_pair = pseudocounts.freqs[1]


        #compute counts and scale them accordingly to size of original MSA
        sample_counts_single    = sampled_freq_single * self.Ni[:, np.newaxis]
        sample_counts_pair      = sampled_freq_pair * self.Nij[:, :, np.newaxis, np.newaxis]

        #actually compute the gradients
        g_single = sample_counts_single - self.msa_counts_single
        g_pair = sample_counts_pair - self.msa_counts_pair

        #sanity check
        if(np.abs(np.sum(sample_counts_single[1,:21]) - np.sum(self.msa_counts_single[1,:21])) > 1e-5):
            print("Warning: sample aa counts ({0}) do not equal input msa aa counts ({1})!".format(
                np.sum(sample_counts_single[1,:21]), np.sum(self.msa_counts_single[1,:21]))
            )

        # set gradients for gap states to 0
        #g_single[:, 20] = 0
        #g_pair[:, :, :, 20] = 0
        #g_pair[:, :, 20, :] = 0

        # set diagonal elements to 0
        for i in range(self.ncol):
            g_pair[i, i, :, :] = 0

        #compute regularization
        x_single, x_pair = self.linear_to_structured(x)                     #x_single has dim L x 20
        _, g_single_reg, g_pair_reg = self.regularization(x_single, x_pair) #g_single_reg has dim L x 20

        #gradient for x_single only L x 20
        g = self.structured_to_linear(g_single[:, :21], g_pair)
        g_reg = self.structured_to_linear(g_single_reg[:, :21], g_pair_reg)

        return -1, g, g_reg

    def get_parameters(self):
        parameters = {}
        parameters['gibbs_steps'] = int(self.gibbs_steps)
        parameters['nr_seq_sample'] = int(self.nr_seq_sample)


        return parameters
