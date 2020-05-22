import os
import re
import warnings
import scipy.io
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from anndata import AnnData
from scipy.stats import norm
from scipy.optimize import minimize
from scipy.sparse import csr_matrix, isspmatrix

from .pyliger import Liger
from .utilities import MergeSparseDataAll


def read10X(sample_dirs, 
            sample_names, 
            merge = True, 
            num_cells = None, 
            min_umis = 0,
            use_filtered = False, 
            reference = None, 
            data_type = "rna"):
    """ Read 10X alignment data (including V3)    
     
    This function generates a sparse matrix (genes x cells) from the data generated by 10X's
    cellranger count pipeline. It can process V2 and V3 data together, producing either a single
    merged matrix or list of matrices. Also handles multiple data types produced by 10X V3 (Gene
    Expression, Antibody Capture, CRISPR, CUSTOM).
     
    Args:
        sample_dirs(list):
            List of directories containing either matrix.mtx(.gz) file along with genes.tsv,
            (features.tsv), and barcodes.tsv, or outer level 10X output directory (containing outs directory).
        sample_names(list): 
            List of names to use for samples (corresponding to sample_dirs)
        merge(bool): optional, (default True)
            Whether to merge all matrices of the same data type across samples or leave as list
            of matrices.
        num_cells(int): optional, (default None)
            Optional limit on number of cells returned for each sample (only for Gene
            Expression data). Retains the cells with the highest numbers of transcripts.
        min_umis(int): optional, (default 0)
            Minimum UMI threshold for cells.
        use_filtered(bool): optional, (default Flase)
            Whether to use 10X's filtered data (as opposed to raw). Only relevant for
            sample.dirs containing 10X outs directory.
        reference(): optional, (default None)
            For 10X V<3, specify which reference directory to use if sample_dir is outer
            level 10X directory (only necessary if more than one reference used for sequencing).
        data_type(str): optional, 'rna' or 'atac', (default 'rna')
            Indicates the protocol of the input data. If not specified, input data will be 
            considered scRNA-seq data. 

    Return:
        datalist(list): 
             List of merged matrices stored as AnnData objects across data types 
             (returns sparse matrix if only one data type detected), or nested 
             list of matrices organized by sample if merge=F.
         
    Usage:
         >>> sample_dir1 = "path/to/outer/dir1" # 10X output directory V2 -- contains outs/raw_gene_bc_matrices/<reference>/...
         >>> sample_dir2 = "path/to/outer/dir2" # 10X output directory V3 -- for two data types, Gene Expression and CUSTOM
         >>> dges1 = read10X(list(sample_dir1, sample_dir2), c("sample1", "sample2"), min.umis = 50)
         >>> ligerex = createLiger(expr = dges1[["Gene Expression"]], custom = dges1[["CUSTOM"]])
    """
    datalist = []
    datatypes = np.array(['Gene Expression'])
    
    if num_cells is not None:
        num_cells = np.repeat(num_cells, len(sample_dirs))
    
    for i in range(len(sample_dirs)):
        
        # Start message
        print('Processing sample ' + sample_names[i])
        
        # Construct sample path
        sample_dir = sample_dirs[i]
        inner1 = sample_dir + '/outs'

        if os.path.exists(inner1):
            sample_dir = inner1
            is_v3 = os.path.exists(sample_dir + '/filtered_feature_bc_matrix')
            matrix_prefix = str(np.where(use_filtered, 'filtered', 'raw'))
            if is_v3:
                sample_dir = sample_dir + '/' + matrix_prefix + '_feature_bc_matrix'
            else:
                if reference is None:
                    references = os.listdir(sample_dir + '/raw_gene_bc_matrices')
                    if len(references) > 1:
                        raise ValueError('Multiple reference genomes found. Please specify a single one.')
                    else:
                        reference = references[0]
            if reference is None:
                reference = ''
            sample_dir = sample_dir + '/' + matrix_prefix + '_gene_bc_matrices/' + reference
        else:
            is_v3 = os.path.exists(sample_dir + '/features.tsv.gz')
        
        suffix = str(np.where(is_v3, '.gz', ''))
        if data_type == 'rna':
            features_file = str(np.where(is_v3, sample_dir + '/features.tsv.gz', sample_dir + '/genes.tsv'))
        elif data_type == 'atac':
            features_file = str(np.where(is_v3, sample_dir + '/peaks.bed.gz', sample_dir + '/peaks.bed'))
        
        matrix_file = sample_dir + '/matrix.mtx' + suffix
        barcodes_file = sample_dir + "/barcodes.tsv" + suffix
        
        # Read in raw data (count matrix)
        rawdata = scipy.io.mmread(matrix_file)
        rawdata = csr_matrix(rawdata) # convert to csr matrix
        
        # filter for UMIs first to increase speed
        umi_pass = np.sum(rawdata, axis=0) > min_umis 
        umi_pass = np.asarray(umi_pass).flatten() # convert to np array
        if umi_pass.shape[0] == 0:
            print('No cells pass UMI cutoff. Please lower it.')
        rawdata = rawdata[:,umi_pass]
        
        # Create column names
        barcodes = pd.read_csv(barcodes_file, sep='\t', header=None)
        barcodes = barcodes.to_numpy().flatten()[umi_pass]
  
        # remove -1 tag from barcodes
        for i in range(barcodes.size):
            barcodes[i] = re.sub('\-1$', '', barcodes[i])
            
        col_names = pd.DataFrame(barcodes, columns=['barcodes'])
        
        # Create row names
        if data_type == 'rna':
            features = pd.read_csv(features_file, sep='\t', header=None).to_numpy() # convert to np array
            row_names = features[:,1]
            
            # TODO: change to anndata function
            # equal to make.unique function in R
            count_dict = {}
            for i in range(len(row_names)):
                name = row_names[i]
                if name not in count_dict:
                    count_dict[name] = 0
                if name in row_names:
                    count_dict[name] += 1
                    if count_dict[name] > 1:
                        row_names[i] = row_names[i] + '.' + str(count_dict[name]-1)
            
        elif data_type == 'atac':
            features = pd.read_csv(features_file, sep='\t', header=None).to_numpy()
            features = np.array([str(feature[0]) + ':' + str(feature[1]) + '-' + str(feature[2]) for feature in features])
            row_names = pd.DataFrame(features, columns=['gene_name'])
        

        # split based on 10X datatype -- V3 has Gene Expression, Antibody Capture, CRISPR, CUSTOM
        # V2 has only Gene Expression by default and just two columns
        # TODO: check atac feature file
        if features.shape[1] == 1: 
            sample_datatypes = np.array(['Chromatin Accessibility'])
            adata = AnnData(csr_matrix(rawdata), obs=row_names, var=col_names)
            adata.uns['sample_name'] = sample_names[i]
            adata.uns['data_type'] = 'Chromatin Accessibility'
            datalist.append(adata)
        elif features.shape[1] < 3:
            sample_datatypes = np.array(['Gene Expression'])
            adata = AnnData(csr_matrix(rawdata), obs=row_names, var=col_names)
            adata.uns['sample_name'] = sample_names[i]
            adata.uns['data_type'] = 'Gene Expression'
            datalist.append(adata)
        else:
            sample_datatypes = features[:,2]
            sample_datatypes_unique = np.unique(sample_datatypes)
            # keep track of all unique datatypes
            datatypes = np.union1d(datatypes, sample_datatypes_unique)
            
            for name in sample_datatypes:
                idx = sample_datatypes == name
                subset_row_names = row_names[idx]
                subset_row_names = pd.DataFrame(subset_row_names, columns=['gene_name'])
                subset_data = rawdata[:,sample_datatypes == name]
                adata = AnnData(csr_matrix(subset_data), obs=subset_row_names, var=col_names)
                adata.uns['sample_name'] = sample_names[i]
                adata.uns['data_type'] = name
                datalist.append(adata)
                
        # num_cells filter only for gene expression data
#        if num_cells is not None:
#            if 'Gene Expression' or 'Chromatin Accessibility' in sample_datatypes and sample_datatypes.shape[0] == 1:
#                data_label = sample_datatypes.item()
#                cs = np.sum(samplelist[data_label], axis=0)
#                limit = samplelist[data_label].shape[1]
#                if num_cells[i] > limit:
#                    print('You selected more cells than are in matrix {}. Returning all {} cells.'.format(i, limit))
#                num_cells[i] = limit
#                samplelist[data_label] = np.flip(np.sort(samplelist[data_label]))[0:num_cells[i]]
        
    
    return_dges = {}
    for datatype in datatypes:
        for data in datalist:
            if datatype not in return_dges:
                return_dges[datatype] = []
            else:
                return_dges[datatype].append(data[data])
        
        return_dges.append(adata)
    if merge:
        print('Merging samples')
        
            
            #return_dges = MergeSparseDataAll()
        # if only one type of data present
        if len(return_dges) == 1:
            print('Returning {} data matrix'.format(datatypes))
            
    else:
        return datalist
        
    return datalist

def createLiger(adata_list, 
                make_sparse = True, 
                take_gene_union = False,
                remove_missing = True):
    """ Create a liger object. 
    
    This function initializes a liger object with the raw data passed in. It requires a list of
    expression (or another single-cell modality) matrices (gene by cell) for at least two datasets.
    By default, it converts all passed data into Compressed Sparse Row matrix (CSR matrix) to reduce 
    object size. It initializes cell_data with nUMI and nGene calculated for every cell.
    
    Args:
        adata_list(list): 
            List of AnnData objects which store expression matrices (gene by cell). 
            Should be named by dataset.
        make_sparse(bool): optional, (default True) 
            Whether to convert raw_data into sparse matrices.
        take_gene_union(bool): optional,  (default False) 
            Whether to fill out raw_data matrices with union of genes across all
            datasets (filling in 0 for missing data) (requires make_sparse=True).
        remove_missing(bool): optional, (default True)
            Whether to remove cells not expressing any measured genes, and genes not
            expressed in any cells (if take_gene_union=True, removes only genes not 
            expressed in any dataset).
        
    Return:
        liger_object(liger): 
            object with raw_data slot set.
    
    Usage:
        >>> adata1 = AnnData(np.arange(12).reshape((4, 3)))
        >>> adata2 = AnnData(np.arange(12).reshape((4, 3)))
        >>> ligerex = createLiger([adata1, adata2])
        
    """ 
    # Make matrix sparse
    if make_sparse:
        for i in range(len(adata_list)):
            if isspmatrix(adata_list[i].X):
                # forse raw data to be csr matrix
                adata_list[i].X = csr_matrix(adata_list[i].X)      
                # check if dimnames exist
                if not adata_list[i].obs_keys() or not adata_list[i].var_keys():
                    raise ValueError('Raw data must have both row (gene) and column (cell) names.')
                # check whether cell name is unique or not
                if adata_list[i].var['barcodes'].shape[0] - np.unique(adata_list[i].var['barcodes']).shape[0] > 0 and adata_list[i].X.shape[0] > 1:  
                    raise ValueError('At least one cell name is repeated across datasets; please make sure all cell names are unique.')
            else:
                adata_list[i].X = csr_matrix(adata_list[i].X)        
    
    # Take gene union (requires make_sparse=True)
    if take_gene_union and make_sparse:
        merged_data = MergeSparseDataAll(adata_list)
        if remove_missing:
            missing_genes = np.array(np.sum(merged_data.X, axis=1)).flatten() == 0
            if np.sum(missing_genes) > 0:
                print('Removing {} genes not expressed in any cells across merged datasets.'.format(np.sum(missing_genes)))
                # show gene name when the total of missing genes is less than 25
                if np.sum(missing_genes) < 25:
                    print(merged_data.obs['gene_name'][missing_genes])
                # save data after removing missing genes
                merged_data = merged_data[~missing_genes,:].copy()
        # fill out raw_data matrices with union of genes across all datasets
        for i in range(len(adata_list)):
            adata_list[i] = merged_data[:, merged_data.var['barcodes']==adata_list[i].var['barcodes']].copy()
    
    # Create liger object based on raw data list
    liger_object = Liger(adata_list)
    
    # Remove missing cells
    if remove_missing:
        liger_object = removeMissingObs(liger_object, use_cols = True)
        # remove missing genes if not already merged
        if not take_gene_union:
            liger_object = removeMissingObs(liger_object, use_cols = False)
    
    # Initialize cell_data for liger_object with nUMI, nGene, and dataset
    liger_object.cell_data = pd.DataFrame()
    for adata in adata_list:
        temp = pd.DataFrame(index=adata.var['barcodes'])
        temp['nUMI'] = np.array(np.sum(adata.X, axis=0)).flatten()
        temp['nGene'] = np.count_nonzero(adata.X.toarray(), axis=0)
        temp['dataset'] = np.repeat(adata.uns['sample_name'], adata.var['barcodes'].shape[0])
        liger_object.cell_data.append(temp)
    
    return liger_object


def normalize(liger_object):
    """ Normalize raw datasets to column sums
    
    This function normalizes data to account for total gene expression across a cell.
    
    Args:
        liger_object(liger): 
            liger object with raw_data
    
    Return:
        liger_object(liger):
            liger object with norm_data
            
    Usage:
        >>> adata1 = AnnData(np.arange(12).reshape((4, 3)))
        >>> adata2 = AnnData(np.arange(12).reshape((4, 3)))
        >>> ligerex = createLiger([adata1, adata2])
        >>> ligerex = normalize(ligerex)
    """
    liger_object = removeMissingObs(liger_object, slot_use='raw_data', use_cols=True)
    
    for i in range(len(liger_object.adata_list)):
        liger_object.adata_list[i].layers['norm_data'] = csr_matrix(liger_object.adata_list[i].X/np.sum(liger_object.adata_list[i].X, axis=0))
    
    return liger_object


def selectGenes(liger_object,
                var_thresh = 0.1,
                alpha_thresh = 0.99,
                num_genes = None,
                tol = 0.0001,
                datasets_use = None,
                combine = 'union',
                keep_unique = False,
                capitalize = False, 
                do_plot = False,
                cex_use = 0.3):
    """ Select a subset of informative genes
    
    This function identifies highly variable genes from each dataset and combines these gene sets
    (either by union or intersection) for use in downstream analysis. Assuming that gene
    expression approximately follows a Poisson distribution, this function identifies genes with
    gene expression variance above a given variance threshold (relative to mean gene expression).
    It also provides a log plot of gene variance vs gene expression (with a line indicating expected
    expression across genes and cells). Selected genes are plotted in green.
    
    Args:
        liger_object(liger):
            Should have already called normalize.
        var_thresh(float): optional, (default 0.1)
            Variance threshold. Main threshold used to identify variable genes. Genes with
            expression variance greater than threshold (relative to mean) are selected.
            (higher threshold -> fewer selected genes). Accepts single value or vector with separate
            var_thresh for each dataset.
        alpha_thresh(float): optional, (default 0.99)
            Alpha threshold. Controls upper bound for expected mean gene expression
            (lower threshold -> higher upper bound).
        num_genes(): optional, (default=None)
            Number of genes to find for each dataset. Optimises the value of var_thresh
            for each dataset to get this number of genes. Accepts single value or vector with same length
            as number of datasets.
        tol(float): optional, (default 0.0001)
            Tolerance to use for optimization if num.genes values passed in.
        datasets_use(list): optional, (default 1:len(liger_object.raw_data))
            List of datasets to include for discovery of highly variable genes. 
        combine(str): optional, 'union' or 'intersect', (default 'union')
            How to combine variable genes across experiments.
        keep_unique(bool): optional, (default False)
            Keep genes that occur (i.e., there is a corresponding column in raw_data) only
            in one dataset.
        capitalize(bool): optional, (default False)
            Capitalize gene names to match homologous genes (ie. across species)
        do_plot(bool): optional, (default False)
            Display log plot of gene variance vs. gene expression for each dataset.
            Selected genes are plotted in green.
        cex_use(float): optional, (default 0.3)
            Point size for plot.
            
    Return:
        liger_object(liger): 
            Object with var_genes attribute.
            
    Usage:
        >>> adata1 = AnnData(np.arange(12).reshape((4, 3)))
        >>> adata2 = AnnData(np.arange(12).reshape((4, 3)))
        >>> ligerex = createLiger([adata1, adata2])
        >>> ligerex = normalize(ligerex)
        >>> ligerex = selectGenes(ligerex) # use default selectGenes settings
        >>> ligerex = selectGenes(ligerex, var_thresh=0.8) # select a smaller subset of genes
    """
    if datasets_use is None:
        datasets_use = list(range(len(liger_object.adata_list)))
        
    # Expand if only single var_thresh passed
    if isinstance(var_thresh, int) or isinstance(var_thresh, float):
        var_thresh = np.repeat(var_thresh, len(liger_object.adata_list))
    if num_genes is not None:
        num_genes = np.repeat(num_genes, len(liger_object.adata_list))
    
    if not np.array_equal(np.intersect1d(datasets_use, list(range(len(liger_object.adata_list)))), datasets_use):
        datasets_use = np.intersect1d(datasets_use, list(range(len(liger_object.adata_list))))
        
    genes_use = np.array([])
    for i in datasets_use:
        if capitalize:
            liger_object.adata_list[i].obs['gene_name'] = liger_object.adata_list[i].obs['gene_name'].str.upper()
            
        trx_per_cell = np.array(np.sum(liger_object.adata_list[i].X, axis=0)).flatten()
        # Each gene's mean expression level (across all cells)
        gene_expr_mean = np.array(np.mean(liger_object.adata_list[i].layers['norm_data'], axis=1)).flatten()
        # Each gene's expression variance (across all cells)
        gene_expr_var = np.array(np.var(liger_object.adata_list[i].layers['norm_data'].toarray(), axis=1)).flatten()
        
        nolan_constant = np.mean(1/trx_per_cell)
        alphathresh_corrected = alpha_thresh / liger_object.adata_list[i].shape[0]
        
        genemeanupper = gene_expr_mean + norm.ppf(1 - alphathresh_corrected / 2) * np.sqrt(gene_expr_mean * nolan_constant / liger_object.adata_list[i].shape[1])
        
        basegenelower = np.log10(gene_expr_mean * nolan_constant)

        def num_varGenes(x, num_genes_des):
            # This function returns the difference between the desired number of genes and
            # the number actually obtained when thresholded on x
            y = np.sum(gene_expr_var / nolan_constant > genemeanupper & np.log10(gene_expr_var) > basegenelower + x)
            return np.abs(num_genes_des - y)
        
        if num_genes is not None:
        # Optimize to find value of x which gives the desired number of genes for this dataset
        # if very small number of genes requested, var.thresh may need to exceed 1
            
            optimized = minimize(fun=num_varGenes, x0=[0], agrs=num_genes[i], tol=tol, bounds=[(0,1.5)])
            var_thresh[i] = optimized.x
            if var_thresh[i].shape[0] > 1:
                warnings.warn('Returned number of genes for dataset {} differs from requested by {}. Lower tol or alpha_thresh for better results.'.format(i, optimized.x.shape[0])) 

        select_gene = (gene_expr_var / nolan_constant > genemeanupper) & (np.log10(gene_expr_var) > basegenelower + var_thresh[i])
        genes_new = liger_object.adata_list[i].obs['gene_name'].to_numpy()[select_gene]
        
        # TODO: needs to be improved
        if do_plot:
            plt.plot(np.log10(gene_expr_mean), np.log10(gene_expr_var))
            plt.title(liger_object.adata_list[i].uns['sample_name'])
            plt.xlabel('Gene Expression Mean (log10)')
            plt.ylabel('Gene Expression Variance (log10)')
            
            plt.plot(np.log10(gene_expr_mean), np.log10(gene_expr_mean)+nolan_constant, c='p')
            plt.scatter(np.log10(gene_expr_mean[select_gene]), np.log10(gene_expr_var[select_gene]), c='g')
            plt.legend('Selected genes: '+str(len(genes_new)), loc='best')

            plt.show()
            
        if combine == 'union':
            genes_use = np.union1d(genes_use, genes_new)
        
        if combine == 'intersect':
            if genes_use.shape[0] == 0:
                genes_use = genes_new
            genes_use = np.intersect1d(genes_use, genes_new)
          
    if not keep_unique:
        for i in range(len(liger_object.adata_list)):
            genes_use = np.intersect1d(genes_use, liger_object.adata_list[i].obs['gene_name'])    
    
    if genes_use.shape[0] == 0:
        warnings.warn('No genes were selected; lower var_thresh values or choose "union" for combine parameter')
            
    liger_object.var_genes = genes_use

    return liger_object



def scaleNotCenter(liger_object, 
                   remove_missing = True):
    """ Scale genes by root-mean-square across cells
    
    This function scales normalized gene expression data after variable genes have been selected.
    Note that the data is not mean-centered before scaling because expression values must remain
    positive (NMF only accepts positive values). It also removes cells which do not have any
    expression across the genes selected, by default.
    
    Args:
        liger_object(liger):
            Should call normalize and selectGenes before calling.
        remove_missing(bool): optional, (default True)
            Whether to remove cells from scale_data with no gene expression.
            
    Return:
        liger_object(liger):
            Object with scale_data layer.
            
    Usage:
        >>> adata1 = AnnData(np.arange(12).reshape((4, 3)))
        >>> adata2 = AnnData(np.arange(12).reshape((4, 3)))
        >>> ligerex = createLiger([adata1, adata2])
        >>> ligerex = normalize(ligerex)
        >>> ligerex = selectGenes(ligerex) # select genes
        >>> ligerex = scaleNotCenter(ligerex)
    """
    for i in range(len(liger_object.adata_list)):
        idx = liger_object.adata_list[i].obs['gene_name'].isin(liger_object.var_genes).to_numpy()
        liger_object.adata_list[i] = liger_object.adata_list[i][idx,].copy()
        
        if isspmatrix(liger_object.adata_list[i].X):
            temp_norm = liger_object.adata_list[i].layers['norm_data']
            liger_object.adata_list[i].layers['scale_data'] = csr_matrix(temp_norm.transpose()/np.sqrt(np.sum(np.square(temp_norm.toarray()), axis=1)/(temp_norm.shape[1]-1))).transpose()
    
    
    if remove_missing:
        liger_object = removeMissingObs(liger_object, slot_use='scale_data', use_cols=False)
    return liger_object


def removeMissingObs(liger_object, 
                     slot_use = 'raw_data', 
                     use_cols = True):
    """ Remove cells/genes with no expression across any genes/cells
    
    Removes cells/genes from chosen slot with no expression in any genes or cells respectively.
    
    Args:
        liger_object(liger): 
            object (scale_data or norm_data must be set).
        slot_use(str): optional, 'raw_data' or 'scale_data', (default 'raw_data')
            The data slot to filter.
        use_cols(bool): optional, (default True)
            Treat each column as a cell.
        
    Return:
        liger_object(liger): 
            object with modified raw_data (or chosen slot) (dataset names preserved).
        
    Usage:
        >>> ligerex = removeMissingObs(ligerex)
    """
    removed = str(np.where(slot_use in ['raw_data', 'norm_data'] and use_cols == True, 'cells', 'genes'))
    expressed = str(np.where(removed == 'cells', ' any genes', ''))
    
    for i in range(len(liger_object.adata_list)):
        data_type = liger_object.adata_list[i].uns['sample_name']
        if slot_use == 'raw_data':
            filter_data = liger_object.adata_list[i].X
        elif slot_use == 'scale_data':
            filter_data = liger_object.adata_list[i].layers['scale_data']
        
        if use_cols:
            missing = np.array(np.sum(filter_data, axis=0)).flatten() == 0
        else:
            missing = np.array(np.sum(filter_data, axis=1)).flatten() == 0
        if np.sum(missing) > 0:
            print('Removing {} {} not expressing{} in {}.'.format(np.sum(missing), removed, expressed, data_type))
            if use_cols:
                # show gene name when the total of missing is less than 25
                if np.sum(missing) < 25:
                    print(liger_object.adata_list[i].obs['gene_name'][missing])
                liger_object.adata_list[i] = liger_object.adata_list[i][:, ~missing].copy()
            else:
                # show cell name when the total of missing is less than 25
                if np.sum(missing) < 25:
                    print(liger_object.adata_list[i].var['barcode'][missing])
                liger_object.adata_list[i] = liger_object.adata_list[i][~missing, :].copy()

    return liger_object
