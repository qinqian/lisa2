from lisa.core.utils import LoadingBar
from lisa.core.assays import LISA_RP_Assay, delta_RP_wrapper
from multiprocessing import Pool
import numpy as np
from scipy import stats, sparse
import os
from lisa.lisa_public_data.models import UnweightedChromatinModel
from collections import defaultdict

class PeakRP_Assay(LISA_RP_Assay):

    def __init__(self, *, generate_rp_matrix, **kwargs):
        super().__init__(**kwargs)
        self.generate_rp_matrix = generate_rp_matrix

    def predict(self, gene_mask, label_vector, data_object = None, debug = False):

        try:
            self.rp_matrix
        except AttributeError:
            if self.generate_rp_matrix or data_object is None:
                log.append('Generating new RP matrix ...')
                self.rp_matrix = self.make_rp_matrix(self.factor_binding, self.rp_map)
            else:
                self.rp_matrix, _ = self.load_rp_matrix(data_object)
            
        self.log.append('Calculating {} peak-RP p-values ...'.format(self.technology))
        
        #calculate p-values by directly applying mannu-test on RP matrix. Subset the RP matrix for genes-of-interest if required
        p_vals = self.get_delta_RP_p_value(self.rp_matrix[gene_mask, :], label_vector)

        return p_vals

    def get_info(self):
        return dict()

#Generator that repeatedly yields the same factor_binding and rp_map matrices with a new accessibility profile 
#to the multiprocessing pool creator. This reduces data redundancy.
class KnockoutGenerator:

    def __init__(self, accessibility_profiles, factor_binding, rp_map):
        self.accessibility_profiles = accessibility_profiles
        self.rp_map = rp_map
        self.factor_binding = factor_binding

    def __iter__(self):
        for profile in self.accessibility_profiles.T:
            yield profile, self.factor_binding, self.rp_map
    
class Accesibility_Assay(LISA_RP_Assay):

    def __init__(self, *, selection_model, chromatin_model, factor_gene_mask, cores, generate_rp_matrix, **kwargs):
        super().__init__(**kwargs)
        self.selection_model = selection_model
        self.chromatin_model = chromatin_model
        self.factor_gene_mask = factor_gene_mask
        self.cores = cores
        self.generate_rp_matrix = generate_rp_matrix

    def get_info(self):
        return dict(
            selection_model = self.selection_model.get_info(),
            chromatin_model = self.chromatin_model.get_info(),
            selected_datasets = self.metadata.select(list(self.selected_dataset_ids)),
            factor_acc_z_scores = self.factor_acc_z_scores
        )

    def load_accessibility_profiles(self, data_object, selected_dataset_ids):

        loadingbar = LoadingBar('Reading {} data'.format(self.technology), len(selected_dataset_ids), 20)

        accessibility_profiles = []
        for selected_dataset in selected_dataset_ids:
            self.log.append(loadingbar, update_line = True)
            accessibility_profiles.append(
                data_object[self.config.get('accessibility_assay','binned_reads')\
                .format(technology = self.technology, dataset_id = selected_dataset)][...][:,np.newaxis]
            )

        accessibility_profiles = np.concatenate(accessibility_profiles, axis = 1)

        return accessibility_profiles

    #iterator for distributing these matrices to multiple cores
    

    def calculate_ISDs(self, accessibility_profiles, factor_binding, rp_map): #enforced kwargs
        """
        accessibility_profiles: (bins, num_datasets): list of chromatin-accessiblity datasets on which to analyze the effects of ISD
        factor_binding: (num_bins, TFs), a sparse binary map showing bins that contain a chip-seq or motif peak to knock out
        rp_map: (num_bins, genes), precalculated matrix mapping the reads in a bin to a regulatory score for each gene (this is a huge matrix)

        returns:
        delta_regulatory_score (genes x TFs)
        """
        self.log.append('Performing in-silico knockouts ...')

        with Pool(self.cores) as p:
            knockouts = list(p.imap(delta_RP_wrapper, iter(KnockoutGenerator(accessibility_profiles, factor_binding, rp_map))))
        
        #concatenate datasets to for gene x TF x datasets shaped matrix
        self.log.append('Calculating Δ regulatory score ...')
        datacube = np.concatenate(knockouts, axis = 1)

        num_genes, _, num_TFs = datacube.shape

        delta_regulation_score = self.chromatin_model.get_deltaRP_activation(datacube)
        
        assert(delta_regulation_score.shape == (num_genes, num_TFs))

        return delta_regulation_score, datacube

    def introsect_accessibility(self, rp_matrix, gene_mask, dataset_mask, dataset_coefs):

        #print(rp_matrix.shape, gene_mask.shape, gene_mask.dtype)

        factor_accessiblities = np.log2(rp_matrix[gene_mask, :] + 1)

        background_datasets = factor_accessiblities[:, ~dataset_mask]

        factor_means, factor_stds = background_datasets.mean(axis = 1, keepdims = True), background_datasets.std(axis = 1, keepdims = True)
        factor_acc_z_scores = (factor_accessiblities[:, dataset_mask] - factor_means)/factor_stds

        dataset_coefs = dataset_coefs.reshape(-1)
        dataset_coefs_mask = dataset_coefs > 0
        if dataset_coefs_mask.sum() == 0:
            dataset_coefs_mask = np.ones_like(dataset_coefs).astype(np.bool)

        coef_weights = dataset_coefs[dataset_coefs_mask]/dataset_coefs[dataset_coefs_mask].sum()
        factor_acc_z_scores = np.mean(np.multiply(factor_acc_z_scores[:, dataset_coefs_mask], coef_weights.reshape(1,-1)), axis = 1)

        return factor_acc_z_scores

    def predict(self, gene_mask, label_vector, data_object = None, debug = False):

        with self.log.section('Modeling {} purturbations:'.format(self.technology)):

            try:
                self.rp_matrix
            except AttributeError:
                if self.generate_rp_matrix or data_object is None:
                    #log.append('Generating new RP matrix ...')
                    #self.rp_matrix = self.make_rp_matrix(self.factor_binding, self.rp_map)
                    raise AssertionError('One can only use other RP maps with the "Direct" assay.')
                else:
                    self.rp_matrix, self.dataset_ids = self.load_rp_matrix(data_object)

            subset_rp_matrix = self.rp_matrix[gene_mask, :]

            #DNase model building and purturbation
            self.log.append('Selecting discriminative datasets and training chromatin model ...')

            #select the most discriminative datasets
            dataset_mask = self.selection_model.fit(subset_rp_matrix, label_vector)
            #subset the best datasets

            subset_rp_matrix, self.selected_dataset_ids = subset_rp_matrix[:, dataset_mask], self.dataset_ids[dataset_mask]
            #fit the chromatin model to these datasets
            self.chromatin_model.fit(subset_rp_matrix, label_vector, self.cores)
                    
            with self.log.section('Calculating in-silico deletions:'):

                accesibility_profiles = self.load_accessibility_profiles(data_object, self.selected_dataset_ids)

                subset_accessibility, subset_factor_binding, subset_rp_map = self.make_subset_rp_map(rp_map = self.rp_map, 
                    factor_binding = self.factor_binding, gene_mask = gene_mask, accessibility = accesibility_profiles)
                
                delta_reg_scores, datacube = self.calculate_ISDs(subset_accessibility, subset_factor_binding, subset_rp_map)

                self.log.append('Calculating p-values ...')
                
                p_vals = self.get_delta_RP_p_value(delta_reg_scores, label_vector)
        
            
            self.log.append('Introspecting accessibility around factors ...')
            self.factor_acc_z_scores = self.introsect_accessibility(self.rp_matrix, self.factor_gene_mask, dataset_mask,
                self.chromatin_model.model.coef_)

            self.log.append('Done!')

        if debug:
            return p_vals, dict(
                gene_mask = gene_mask,
                label_vector = label_vector,
                subset_rp_matrix = subset_rp_matrix,
                subset_rp_map = subset_rp_map,
                subset_factor_binding = subset_factor_binding,
                datacube = datacube,
                delta_reg_scores = delta_reg_scores,
                dataset_mask = dataset_mask,
                bin_mask = bin_mask,
            )
        else:
            return p_vals