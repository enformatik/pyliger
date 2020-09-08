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
from ._utilities import merge_sparse_data_all


def read_10x(sample_dirs,
             sample_names,
             merge=True,
             num_cells=None,
             min_umis=0,
             use_filtered=False,
             reference=None,
             data_type="rna"):
    """Read 10X alignment data (including V3)    
     
    This function generates a sparse matrix (cells x genes) from the data generated by 10X's
    cellranger count pipeline. It can process V2 and V3 data together, producing either a single
    merged matrix or list of matrices. Also handles multiple data types produced by 10X V3 (Gene
    Expression, Antibody Capture, CRISPR, CUSTOM).
     
    Parameters
    ----------
    sample_dirs : list
        List of directories containing either matrix.mtx(.gz) file along with genes.tsv,
        (features.tsv), and barcodes.tsv, or outer level 10X output directory (containing outs directory).
    sample_names : list
        List of names to use for samples (corresponding to sample_dirs)
    merge : bool, optional
        Whether to merge all matrices of the same data type across samples or leave as list
        of matrices (the default is True).
    num_cells : int, optional
        Optional limit on number of cells returned for each sample (only for Gene
        Expression data). Retains the cells with the highest numbers of transcripts
        (the default is None).
    min_umis : int, optional
        Minimum UMI threshold for cells (the default is 0).
    use_filtered : bool, optional
        Whether to use 10X's filtered data (as opposed to raw). Only relevant for
        sample.dirs containing 10X outs directory (the default is False).
    reference : str, optional
        For 10X V<3, specify which reference directory to use if sample_dir is outer
        level 10X directory (only necessary if more than one reference used for sequencing)
        (the default is None).
    data_type : str, optional, 'rna' or 'atac'
        Indicates the protocol of the input data. If not specified, input data will be 
        considered scRNA-seq data (the default is 'rna'). 

    Returns
    -------
    datalist : list 
         List of merged matrices stored as AnnData objects across data types 
         (returns sparse matrix if only one data type detected), or nested 
         list of matrices organized by sample if merge=F.
    
    Examples
    --------
    >>> sample_dir1 = "path/to/outer/dir1" # 10X output directory V2 -- contains outs/raw_gene_bc_matrices/<reference>/...
    >>> sample_dir2 = "path/to/outer/dir2" # 10X output directory V3 -- for two data types, Gene Expression and CUSTOM
    >>> dges1 = read10X(list(sample_dir1, sample_dir2), c("sample1", "sample2"), min.umis = 50)
    >>> ligerex = createLiger(expr = dges1[["Gene Expression"]], custom = dges1[["CUSTOM"]])
    """

    datalist = []
    datatypes = np.array(['Gene Expression'])
    num_samples = len(sample_dirs)

    if num_cells is not None:
        num_cells = np.repeat(num_cells, num_samples)

    for i in range(num_samples):
        # start message
        print('Processing sample ' + sample_names[i])

        # file_path = _build_path()
        # Construct sample path
        sample_dir = sample_dirs[i]
        check_inner = sample_dir + '/outs'

        if os.path.exists(check_inner):
            sample_dir = check_inner
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
        raw_data = scipy.io.mmread(matrix_file)

        # filter for UMIs first to increase speed
        umi_pass = np.sum(raw_data, axis=0) > min_umis
        umi_pass = np.asarray(umi_pass).flatten()  # convert to np array
        if umi_pass.shape[0] == 0:
            print('No cells pass UMI cutoff. Please lower it.')
        raw_data = raw_data[:, umi_pass]
        raw_data = csr_matrix(raw_data)  # convert to csr matrix

        # Create column names
        barcodes = pd.read_csv(barcodes_file, sep='\t', header=None)
        barcodes = barcodes.to_numpy().flatten()[umi_pass]

        # remove -1 tag from barcodes
        for i in range(barcodes.size):
            barcodes[i] = re.sub('\-1$', '', barcodes[i])

        col_names = pd.DataFrame(barcodes, columns=['barcodes'])

        # Create row names
        if data_type == 'rna':
            features = pd.read_csv(features_file, sep='\t', header=None).to_numpy()  # convert to np array
            row_names = features[:, 1]

            # equal to make.unique function in R
            count_dict = {}
            for i in range(len(row_names)):
                name = row_names[i]
                if name not in count_dict:
                    count_dict[name] = 0
                if name in row_names:
                    count_dict[name] += 1
                    if count_dict[name] > 1:
                        row_names[i] = row_names[i] + '.' + str(count_dict[name] - 1)

            row_names = pd.DataFrame(row_names, columns=['gene_name'])

        elif data_type == 'atac':
            features = pd.read_csv(features_file, sep='\t', header=None).to_numpy()
            features = np.array(
                [str(feature[0]) + ':' + str(feature[1]) + '-' + str(feature[2]) for feature in features])
            row_names = pd.DataFrame(features, columns=['gene_name'])

        # split based on 10X datatype -- V3 has Gene Expression, Antibody Capture, CRISPR, CUSTOM
        # V2 has only Gene Expression by default and just two columns
        # TODO: check atac feature file
        if features.shape[1] == 1:
            sample_datatypes = np.array(['Chromatin Accessibility'])
            adata = AnnData(csr_matrix(raw_data), obs=row_names, var=col_names)
            adata.uns['sample_name'] = sample_names[i]
            adata.uns['data_type'] = 'Chromatin Accessibility'
            datalist.append(adata)
        elif features.shape[1] < 3:
            sample_datatypes = np.array(['Gene Expression'])
            adata = AnnData(csr_matrix(raw_data), obs=row_names, var=col_names)
            adata.uns['sample_name'] = sample_names[i]
            adata.uns['data_type'] = 'Gene Expression'
            datalist.append(adata)
        else:
            sample_datatypes = features[:, 2]
            sample_datatypes_unique = np.unique(sample_datatypes)
            # keep track of all unique datatypes
            datatypes = np.union1d(datatypes, sample_datatypes_unique)

            for name in sample_datatypes:
                idx = sample_datatypes == name
                subset_row_names = row_names[idx]
                subset_row_names = pd.DataFrame(subset_row_names, columns=['gene_name'])
                subset_data = raw_data[:, sample_datatypes == name]
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

        # return_dges = MergeSparseDataAll()
        # if only one type of data present
        if len(return_dges) == 1:
            print('Returning {} data matrix'.format(datatypes))

    else:
        return datalist

    return datalist


# def _build_path():
#    file_path = []


#    return file_path


def create_liger(adata_list,
                 make_sparse=True,
                 take_gene_union=False,
                 remove_missing=True):
    """Create a liger object. 
    
    This function initializes a liger object with the raw data passed in. It requires a list of
    expression (or another single-cell modality) matrices (cell by gene) for at least two datasets.
    By default, it converts all passed data into Compressed Sparse Row matrix (CSR matrix) to reduce 
    object size. It initializes cell_data with nUMI and nGene calculated for every cell.
    
    Parameters
    ----------
    adata_list : list
        List of AnnData objects which store expression matrices (cell by gene).
        Should be named by dataset.
    make_sparse : bool, optional
        Whether to convert raw_data into sparse matrices (the default is True).
    take_gene_union : bool, optional
        Whether to fill out raw_data matrices with union of genes across all
        datasets (filling in 0 for missing data) (requires make_sparse=True)
        (the default is False).
    remove_missing : bool, optional
        Whether to remove cells not expressing any measured genes, and genes not
        expressed in any cells (if take_gene_union=True, removes only genes not 
        expressed in any dataset) (the default is True).

    Returns
    -------
    liger_object : liger object
        object with raw_data slot set.
    TODO: update the docstring for returns
    
    Examples
    --------
    >>> adata1 = AnnData(np.arange(12).reshape((4, 3)))
    >>> adata2 = AnnData(np.arange(12).reshape((4, 3)))
    >>> ligerex = create_liger([adata1, adata2])
        
    """
    num_samples = len(adata_list)

    # Make matrix sparse
    if make_sparse:
        for i in range(num_samples):
            if isspmatrix(adata_list[i].X):
                # force raw data to be csr matrix
                adata_list[i].X = csr_matrix(adata_list[i].X)
                # check if dimnames exist
                if not adata_list[i].obs_keys() or not adata_list[i].var_keys():
                    raise ValueError('Raw data must have both row (cell) and column (gene) names.')
                # check whether cell name is unique or not
                if adata_list[i].obs['barcodes'].shape[0] - np.unique(adata_list[i].obs['barcodes']).shape[0] > 0 and adata_list[i].X.shape[1] > 1:
                    raise ValueError(
                        'At least one cell name is repeated across datasets; please make sure all cell names are unique.')
            else:
                adata_list[i].X = csr_matrix(adata_list[i].X)

    # Take gene union (requires make_sparse=True)
    if take_gene_union and make_sparse:
        merged_data = merge_sparse_data_all(adata_list)
        if remove_missing:
            missing_genes = np.array(np.sum(merged_data.X, axis=0)).flatten() == 0
            if np.sum(missing_genes) > 0:
                print('Removing {} genes not expressed in any cells across merged datasets.'.format(np.sum(missing_genes)))
                # show gene name when the total of missing genes is less than 25
                if np.sum(missing_genes) < 25:
                    print(merged_data.var['gene_name'][missing_genes])
                # save data after removing missing genes
                merged_data = merged_data[:, ~missing_genes].copy()
        # fill out raw_data matrices with union of genes across all datasets
        for i in range(num_samples):
            adata_list[i] = merged_data[merged_data.obs['barcodes'] == adata_list[i].obs['barcodes'], :].copy()

    # Create liger object based on raw data list
    liger_object = Liger(adata_list)

    # Remove missing cells
    if remove_missing:
        liger_object = _remove_missing_obs(liger_object, use_rows=True)
        # remove missing genes if not already merged
        if not take_gene_union:
            liger_object = _remove_missing_obs(liger_object, use_rows=False)

    # Initialize cell_data for liger_object with nUMI, nGene, and dataset
    liger_object.cell_data = pd.DataFrame()
    for adata in adata_list:
        temp = pd.DataFrame(index=adata.obs['barcodes'])
        temp['nUMI'] = np.array(np.sum(adata.X, axis=1)).flatten()
        temp['nGene'] = np.count_nonzero(adata.X.toarray(), axis=1)
        temp['dataset'] = np.repeat(adata.uns['sample_name'], adata.obs['barcodes'].shape[0])
        liger_object.cell_data.append(temp)

    return liger_object


def normalize(liger_object):
    """Normalize raw datasets to row sums
    
    This function normalizes data to account for total gene expression across a cell.

    Parameters
    ----------
    liger_object : liger object
        liger object with raw_data.

    Returns
    -------
    liger_object : liger object
        liger object with norm_data.

    Examples
    --------
    >>> adata1 = AnnData(np.arange(12).reshape((4, 3)))
    >>> adata2 = AnnData(np.arange(12).reshape((4, 3)))
    >>> ligerex = create_liger([adata1, adata2])
    >>> ligerex = normalize(ligerex)
    """
    num_samples = len(liger_object.adata_list)
    liger_object = _remove_missing_obs(liger_object, slot_use='raw_data', use_rows=True)

    for i in range(num_samples):
        liger_object.adata_list[i].layers['norm_data'] = csr_matrix(liger_object.adata_list[i].X / np.sum(liger_object.adata_list[i].X, axis=1))

    return liger_object


def select_genes(liger_object,
                 var_thresh=0.1,
                 alpha_thresh=0.99,
                 num_genes=None,
                 tol=0.0001,
                 datasets_use=None,
                 combine='union',
                 keep_unique=False,
                 capitalize=False,
                 do_plot=False,
                 cex_use=0.3):
    """Select a subset of informative genes
    
    This function identifies highly variable genes from each dataset and combines these gene sets
    (either by union or intersection) for use in downstream analysis. Assuming that gene
    expression approximately follows a Poisson distribution, this function identifies genes with
    gene expression variance above a given variance threshold (relative to mean gene expression).
    It also provides a log plot of gene variance vs gene expression (with a line indicating expected
    expression across genes and cells). Selected genes are plotted in green.

    Parameters
    ----------
    liger_object : liger object
        Should have already called normalize.
    var_thresh : float, optional
        Variance threshold. Main threshold used to identify variable genes. Genes with
        expression variance greater than threshold (relative to mean) are selected.
        (higher threshold -> fewer selected genes). Accepts single value or vector with separate
        var_thresh for each dataset (the default is 0.1).
    alpha_thresh : float, optional
        Alpha threshold. Controls upper bound for expected mean gene expression
        (lower threshold -> higher upper bound) (the default is 0.99).
    num_genes : int, optional
        Number of genes to find for each dataset. Optimises the value of var_thresh
        for each dataset to get this number of genes. Accepts single value or vector with same length
        as number of datasets (the default is None).
    tol : float, optional
        Tolerance to use for optimization if num.genes values passed in (the default is 0.0001).
    datasets_use : list, optional
        List of datasets to include for discovery of highly variable genes 
        (the default is list(range(len(liger_object.adata_list)))).
    combine : str, optional, 'union' or 'intersect'
        How to combine variable genes across experiments (the default is 'union').
    keep_unique : bool, optional
        Keep genes that occur (i.e., there is a corresponding column in raw_data) only
        in one dataset (the default is False).
    capitalize : bool, optional
        Capitalize gene names to match homologous genes (ie. across species)
        (the default is False).
    do_plot : bool, optional
        Display log plot of gene variance vs. gene expression for each dataset.
        Selected genes are plotted in green (the default is False).
    cex_use : float, optional
        Point size for plot (the default is 0.3).

    Returns
    -------
    liger_object : liger object
        Object with var_genes attribute.

    Examples
    --------
    >>> adata1 = AnnData(np.arange(12).reshape((4, 3)))
    >>> adata2 = AnnData(np.arange(12).reshape((4, 3)))
    >>> ligerex = create_liger([adata1, adata2])
    >>> ligerex = normalize(ligerex)
    >>> ligerex = select_genes(ligerex) # use default selectGenes settings
    >>> ligerex = select_genes(ligerex, var_thresh=0.8) # select a smaller subset of genes
    """
    num_samples = len(liger_object.adata_list)
    
    if datasets_use is None:
        datasets_use = list(range(len(liger_object.adata_list)))

    # Expand if only single var_thresh passed
    if isinstance(var_thresh, int) or isinstance(var_thresh, float):
        var_thresh = np.repeat(var_thresh, num_samples)
    if num_genes is not None:
        num_genes = np.repeat(num_genes, num_samples)

    if not np.array_equal(np.intersect1d(datasets_use, list(range(num_samples))), datasets_use):
        datasets_use = np.intersect1d(datasets_use, list(range(num_samples)))

    genes_use = np.array([])
    for i in datasets_use:
        if capitalize:
            liger_object.adata_list[i].var['gene_name'] = liger_object.adata_list[i].var['gene_name'].str.upper()

        trx_per_cell = np.array(np.sum(liger_object.adata_list[i].X, axis=1)).flatten()
        # Each gene's mean expression level (across all cells)
        gene_expr_mean = np.array(np.mean(liger_object.adata_list[i].layers['norm_data'], axis=0)).flatten()
        # Each gene's expression variance (across all cells)
        gene_expr_var = np.array(np.var(liger_object.adata_list[i].layers['norm_data'].toarray(), axis=0)).flatten()

        nolan_constant = np.mean(1 / trx_per_cell)
        alphathresh_corrected = alpha_thresh / liger_object.adata_list[i].shape[1]

        gene_mean_upper = gene_expr_mean + norm.ppf(1 - alphathresh_corrected / 2) * np.sqrt(
            gene_expr_mean * nolan_constant / liger_object.adata_list[i].shape[0])

        base_gene_lower = np.log10(gene_expr_mean * nolan_constant)

        def num_varGenes(x, num_genes_des):
            # This function returns the difference between the desired number of genes and
            # the number actually obtained when thresholded on x
            y = np.sum((gene_expr_var / nolan_constant) > gene_mean_upper & np.log10(gene_expr_var) > (base_gene_lower + x))
            return np.abs(num_genes_des - y)

        if num_genes is not None:
            # Optimize to find value of x which gives the desired number of genes for this dataset
            # if very small number of genes requested, var.thresh may need to exceed 1

            optimized = minimize(fun=num_varGenes, x0=[0], agrs=num_genes[i], tol=tol, bounds=[(0, 1.5)])
            var_thresh[i] = optimized.x
            if var_thresh[i].shape[0] > 1:
                warnings.warn('Returned number of genes for dataset {} differs from requested by {}. Lower tol or alpha_thresh for better results.'.format(i, optimized.x.shape[0]))

        select_gene = ((gene_expr_var / nolan_constant) > gene_mean_upper) & (np.log10(gene_expr_var) > (base_gene_lower + var_thresh[i]))
        genes_new = liger_object.adata_list[i].var['gene_name'].to_numpy()[select_gene]

        # TODO: graph needs to be improved
        if do_plot:
            plt.plot(np.log10(gene_expr_mean), np.log10(gene_expr_var))
            plt.title(liger_object.adata_list[i].uns['sample_name'])
            plt.xlabel('Gene Expression Mean (log10)')
            plt.ylabel('Gene Expression Variance (log10)')

            plt.plot(np.log10(gene_expr_mean), np.log10(gene_expr_mean) + nolan_constant, c='p')
            plt.scatter(np.log10(gene_expr_mean[select_gene]), np.log10(gene_expr_var[select_gene]), c='g')
            plt.legend('Selected genes: ' + str(len(genes_new)), loc='best')

            plt.show()

        if combine == 'union':
            genes_use = np.union1d(genes_use, genes_new)

        if combine == 'intersect':
            if genes_use.shape[0] == 0:
                genes_use = genes_new
            genes_use = np.intersect1d(genes_use, genes_new)

    if not keep_unique:
        for i in range(num_samples):
            genes_use = np.intersect1d(genes_use, liger_object.adata_list[i].var['gene_name'])

    if genes_use.shape[0] == 0:
        warnings.warn('No genes were selected; lower var_thresh values or choose "union" for combine parameter')

    liger_object.var_genes = genes_use   

    return liger_object


def scale_not_center(liger_object,
                     remove_missing=True):
    """Scale genes by root-mean-square across cells
    
    This function scales normalized gene expression data after variable genes have been selected.
    Note that the data is not mean-centered before scaling because expression values must remain
    positive (NMF only accepts positive values). It also removes cells which do not have any
    expression across the genes selected, by default.
    
    Parameters
    ----------
    liger_object : liger object
        Should call normalize and selectGenes before calling.
    remove_missing : bool, optional
        Whether to remove cells from scale_data with no gene expression
        (the default is True).

    Returns
    -------
    liger_object : liger object
        Object with scale_data layer.

    Examples
    --------
    >>> adata1 = AnnData(np.arange(12).reshape((4, 3)))
    >>> adata2 = AnnData(np.arange(12).reshape((4, 3)))
    >>> ligerex = create_liger([adata1, adata2])
    >>> ligerex = normalize(ligerex)
    >>> ligerex = select_genes(ligerex) # select genes
    >>> ligerex = scale_not_center(ligerex)
    """
    num_sampels = len(liger_object.adata_list)
    
    for i in range(num_sampels):
        idx = liger_object.adata_list[i].var['gene_name'].isin(liger_object.var_genes).to_numpy()
        liger_object.adata_list[i] = liger_object.adata_list[i][:, idx].copy()
        
        temp_norm = liger_object.adata_list[i].layers['norm_data']
        liger_object.adata_list[i].layers['scale_data'] = csr_matrix(temp_norm / np.sqrt(np.sum(np.square(temp_norm.toarray()), axis=0) / (temp_norm.shape[0] - 1)))

    if remove_missing:
        liger_object = _remove_missing_obs(liger_object, slot_use='scale_data', use_rows=False)
    
    return liger_object


def _remove_missing_obs(liger_object,
                        slot_use='raw_data',
                        use_rows=True):
    """Remove cells/genes with no expression across any genes/cells
    
    Removes cells/genes from chosen slot with no expression in any genes or cells respectively.

    Parameters
    ----------
    liger_object : liger object
        object (scale_data or norm_data must be set).
    slot_use : str, optional, 'raw_data' or 'scale_data'
        The data slot to filter (the default is 'raw_data').
    use_rows : bool, optional
        Treat each row as a cell (the default is True).

    Returns
    -------
    liger_object : liger object
        object with modified raw_data (or chosen slot) (dataset names preserved).

    Examples
    --------
    >>> ligerex = _remove_missing_obs(ligerex)
    """
    num_samples = len(liger_object.adata_list)

    removed = str(np.where(slot_use in ['raw_data', 'norm_data'] and use_rows == True, 'cells', 'genes'))
    expressed = str(np.where(removed == 'cells', ' any genes', ''))

    for i in range(num_samples):
        data_type = liger_object.adata_list[i].uns['sample_name']
        if slot_use == 'raw_data':
            filter_data = liger_object.adata_list[i].X
        elif slot_use == 'scale_data':
            filter_data = liger_object.adata_list[i].layers['scale_data']

        if use_rows:
            missing = np.array(np.sum(filter_data, axis=1)).flatten() == 0
        else:
            missing = np.array(np.sum(filter_data, axis=0)).flatten() == 0
        if np.sum(missing) > 0:
            print('Removing {} {} not expressing{} in {}.'.format(np.sum(missing), removed, expressed, data_type))
            if use_rows:
                # show gene name when the total of missing is less than 25
                if np.sum(missing) < 25:
                    print(liger_object.adata_list[i].var['gene_name'][missing])
                liger_object.adata_list[i] = liger_object.adata_list[i][~missing, :].copy()
            else:
                # show cell name when the total of missing is less than 25
                if np.sum(missing) < 25:
                    print(liger_object.adata_list[i].obs['barcode'][missing])
                liger_object.adata_list[i] = liger_object.adata_list[i][:, ~missing].copy()

    return liger_object
