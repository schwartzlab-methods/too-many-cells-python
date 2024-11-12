#########################################################
#Princess Margaret Cancer Research Tower
#Schwartz Lab
#Javier Ruiz Ramirez
#October 2024
#########################################################
#This is a Python script to produce TMC trees using
#the original too-many-cells tool.
#https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7439807/
#########################################################
#Questions? Email me at: javier.ruizramirez@uhn.ca
#########################################################
import os
import numpy as np
import pandas as pd
from typing import List
from typing import Tuple
import matplotlib as mpl
from scipy import spatial
from typing import Optional
from scipy import sparse as sp
from numpy.typing import ArrayLike
from time import perf_counter as clock
from sklearn.metrics import pairwise_distances
from scipy.sparse.linalg import LinearOperator
from sklearn.decomposition import TruncatedSVD
from scipy.sparse.linalg import eigs as EigenGeneral
from sklearn.metrics.pairwise import pairwise_kernels
from scipy.sparse.linalg import eigsh as EigenHermitian
from sklearn.feature_extraction.text import TfidfTransformer

class SimilarityMatrix:

    #=====================================
    def __init__(
            self,
            matrix: ArrayLike,
            use_hermitian_method: bool = False,
            svd_algorithm: str = "randomized",
            verbose_mode: bool = False,
    ):
        self.X: ArrayLike = matrix
        self.is_sparse = sp.issparse(self.X)
        self.similarity_norm: float
        self.trunc_SVD = None
        self.use_hermitian_method = use_hermitian_method
        self.svd_algorithm = svd_algorithm

        list_of_svd_algorithms = ["randomized", "arpack"]
        if svd_algorithm not in list_of_svd_algorithms:
            raise ValueError("Unexpected SVD algorithm.")

        self.verbose_mode = verbose_mode
        self.eps = 1e-9
        

    #=====================================
    def compute_vector_of_norms(self,
                                lp_norm = 2,
        ) -> ArrayLike:
        if self.is_sparse:
            vec = sp.linalg.norm(self.X,
                                 ord=lp_norm,
                                 axis=1)
        else:
            vec = np.linalg.norm(self.X,
                                 ord=lp_norm,
                                 axis=1)

        return vec


    #=====================================
    def compute_similarity_matrix(
            self,
            shift_similarity_matrix: float = 0,
            shift_until_nonnegative: bool = False,
            store_similarity_matrix: bool = False,
            normalize_rows: bool = False,
            similarity_function: str = "cosine_sparse",
            similarity_norm: float = 2,
            similarity_power: float = 1,
            similarity_gamma: Optional[float] = None,
            use_tf_idf: bool = False,
            tf_idf_norm: Optional[str] = None,
            tf_idf_smooth: bool = True,
    ):

        if similarity_norm < 1:
            raise ValueError("Unexpected similarity norm.")
        self.similarity_norm = similarity_norm

        if similarity_gamma is None:
            # gamma = 1 / (number of features)
            similarity_gamma = 1 / self.X.shape[1]
        elif similarity_gamma <= 0:
            raise ValueError("Unexpected similarity gamma.")

        if similarity_power <= 0:
            raise ValueError("Unexpected similarity power.")

        similarity_functions = []
        similarity_functions.append("cosine_sparse")
        similarity_functions.append("norm_sparse")
        similarity_functions.append("cosine")
        similarity_functions.append("neg_exp")
        similarity_functions.append("laplacian")
        similarity_functions.append("gaussian")
        similarity_functions.append("div_by_sum")
        similarity_functions.append("div_by_delta_max")

        if similarity_function not in similarity_functions:
            raise ValueError("Unexpected similarity fun.")

        #TF-IDF section
        if use_tf_idf:

            t0 = clock()
            print("Using inverse document frequency (IDF).")

            if tf_idf_norm is None:
                pass 
            else:
                print("Using term frequency normalization.")
                tf_idf_norms = ["l2","l1"]
                if tf_idf_norm not in tf_idf_norms:
                    raise ValueError("Unexpected tf norm.")

            tf_idf_obj = TfidfTransformer(
                norm=tf_idf_norm,
                smooth_idf=tf_idf_smooth)

            self.X = tf_idf_obj.fit_transform(self.X)
            if self.is_sparse:
                pass
            else:
                #If the matrix was originally dense
                #and the tf_idf function changed it
                #to sparse, then convert to dense.
                if sp.issparse(self.X):
                    self.X = self.X.toarray()

            tf = clock()
            delta = tf - t0
            txt = ("Elapsed time for IDF build: " +
                    f"{delta:.2f} seconds.")
            print(txt)

        #Normalization section
        use_cos_sp = similarity_function == "cosine_sparse"
        use_dbs = similarity_function == "div_by_sum"

        if normalize_rows or use_cos_sp or use_dbs:
            t0 = clock()

            if self.is_sparse:
                self.normalize_sparse_rows()
            else:
                self.normalize_dense_rows()

            tf = clock()
            delta = tf - t0
            txt = ("Elapsed time for normalization: " +
                    f"{delta:.2f} seconds.")
            print(txt)

        #Similarity section.
        print(f"Working with {similarity_function=}")
        similarity_matrix_is_sparse = False

        if similarity_function == "cosine_sparse":

            similarity_matrix_is_sparse = True

            self.trunc_SVD = TruncatedSVD(
                n_components=2,
                n_iter=5,
                algorithm=self.svd_algorithm
            )

        elif similarity_function == "norm_sparse":

            similarity_matrix_is_sparse = True
            vec = self.compute_vector_of_norms()
            self.norm_sq_vec = vec * vec
            diam = self.compute_diameter_for_observations()
            self.inv_diam_sq = 1 / (diam * diam)

        else:
            #Use a similarity function different from
            #the cosine_sparse similarity function.

            t0 = clock()
            print("Building similarity matrix ...")
            n_rows = self.X.shape[0]
            max_workers = int(os.cpu_count())
            n_workers = 1
            if n_rows < 500:
                pass
            elif n_rows < 5000:
                if 8 < max_workers:
                    n_workers = 8
            elif n_rows < 50000:
                if 16 < max_workers:
                    n_workers = 16
            else:
                if 25 < max_workers:
                    n_workers = 25
            print(f"Using {n_workers=}.")

        if similarity_function == "cosine_sparse":
            # This function is not translation invariant.
            pass
        elif similarity_function == "cosine":
            #( x @ y ) / ( ||x|| * ||y|| )
            # This function is not translation invariant.
            self.X = pairwise_kernels(self.X,
                                        metric="cosine",
                                        n_jobs=n_workers)

        elif similarity_function == "neg_exp":
            # exp(-||x-y||^power * gamma)
            # This function is translation invariant.

            # Notice the similarity between this function
            # and the laplacian kernel below. 
            # The Laplacian kernel only offers the L1 norm.
            def sim_fun(x,y):
                delta = np.linalg.norm(
                    x-y, ord=similarity_norm)
                delta = np.power(delta, similarity_power)
                return np.exp(-delta * similarity_gamma)

            self.X = pairwise_kernels(
                self.X,
                metric=sim_fun,
                n_jobs=n_workers)

        elif similarity_function == "laplacian":
            #exp(-||x-y||^power * gamma)
            # This function is translation invariant.
            # The Laplacian kernel only offers the L1 norm.
            def sim_fun(x,y):
                delta = np.linalg.norm(
                    x-y, ord=1)
                delta = np.power(delta, 1)
                return np.exp(-delta * similarity_gamma)

            self.X = pairwise_kernels(
                self.X,
                metric="laplacian",
                n_jobs=n_workers,
                gamma = similarity_gamma)

        elif similarity_function == "gaussian":
            #exp(-||x-y||^power * gamma)
            # This function is translation invariant.
            # The Gaussian kernel only offers the L2 norm.
            def sim_fun(x,y):
                delta = np.linalg.norm(
                    x-y, ord=2)
                delta = np.power(delta, 2)
                return np.exp(-delta * similarity_gamma)

            self.X = pairwise_kernels(
                self.X,
                metric="rbf",
                n_jobs=n_workers,
                gamma = similarity_gamma)

        elif similarity_function == "div_by_sum":
            # D(x,y) = 1 - ||x-y|| / (||x|| + ||y||)
            # This function is not translation invariant.

            # If the vectors have unit norm, then
            # D(x,y) = 1 - ||x-y|| / 2

            # If the user chooses this function, then
            # the row vectors are automatically normalized.

            if self.similarity_norm == 1:
                lp_norm = "l1"
            elif self.similarity_norm == 2:
                lp_norm = "l2"
            else:
                txt = "Similarity norm should be 1 or 2."
                raise ValueError(txt)

            self.X = pairwise_distances(self.X,
                                        metric=lp_norm,
                                        n_jobs=n_workers)
            self.X *= -0.5
            self.X += 1

        elif similarity_function == "div_by_delta_max":
            # Let M be the diameter of the set S.
            # M = max_{x,y in S} {||x-y||}
            # D(x,y) = 1 - ||x-y|| / M
            # This function is translation invariant.
            # Note that D(x,y) is zero
            # when ||x-y|| equals M and is 
            # equal to 1 only when x = y.

            if self.similarity_norm == 1:
                lp_norm = "l1"
            elif self.similarity_norm == 2:
                lp_norm = "l2"
            else:
                txt = "Similarity norm should be 1 or 2."
                raise ValueError(txt)

            self.X = pairwise_distances(self.X,
                                        metric=lp_norm,
                                        n_jobs=n_workers)

            diam = self.X.max()
            self.X *= -1 / diam
            self.X += 1


        if not similarity_matrix_is_sparse:

            if shift_until_nonnegative:
                min_value = self.X.min()
                if min_value < 0:
                    shift_similarity_matrix = -min_value
                    txt="Similarity matrix will be shifted."
                    print(txt)
                    txt=f"Shift: {shift_similarity_matrix}."
                    print(txt)
                    self.X += shift_similarity_matrix

            elif shift_similarity_matrix != 0:
                print(f"Similarity matrix will be shifted.")
                print(f"Shift: {shift_similarity_matrix}.")
                self.X += shift_similarity_matrix

            if store_similarity_matrix:
                matrix_fname = "similarity_matrix.npy"
                matrix_fname = os.path.join(self.output,
                                            matrix_fname)
                np.save(matrix_fname, self.X)

            
            print("Similarity matrix has been built.")
            tf = clock()
            delta = tf - t0
            delta /= 60
            txt = ("Elapsed time for similarity build: " +
                    f"{delta:.2f} minutes.")
            print(txt)

    #=====================================
    def normalize_sparse_rows(self):
        """
        Divide each row of the count matrix by the \
            given norm. Note that this function \
            assumes that the matrix is in the \
            compressed sparse row format.
        """

        print("Normalizing rows.")

        for i, row in enumerate(self.X):
            data = row.data.copy()
            row_norm  = np.linalg.norm(
                data, ord=self.similarity_norm)
            data /= row_norm
            start = self.X.indptr[i]
            end   = self.X.indptr[i+1]
            self.X.data[start:end] = data

    #=====================================
    def normalize_dense_rows(self):
        """
        Divide each row of the count matrix by the \
            given norm. Note that this function \
            assumes that the matrix is dense.
        """

        print('Normalizing rows.')

        for row in self.X:
            row /= np.linalg.norm(
                row, ord=self.similarity_norm)

    # #=====================================
    # def modularity_to_json(self, Q:float):
    #     return {'_item': None,
    #             '_significance': None,
    #             '_distance': Q}

    # #=====================================
    # def cell_to_json(self, cell_name, cell_number):
    #     return {'_barcode': {'unCell': cell_name},
    #             '_cellRow': {'unRow': cell_number}}

    # #=====================================
    # def cells_to_json(self,rows):
    #     L = []
    #     for row in rows:
    #         cell_id = self.A.obs.index[row]
    #         D = self.cell_to_json(cell_id, row)
    #         L.append(D)
    #     return {'_item': L,
    #             '_significance': None,
    #             '_distance': None}

    #=====================================
    def compute_partition_for_cosp(self,
                                 rows: np.ndarray,
    ) -> Tuple[float, np.ndarray]:
        """
        This function is for sparse matrices.

        Compute the partition of the given set
        of cells. The rows input
        contains the indices of the
        rows we are to partition.
        The algorithm computes a truncated
        SVD and the corresponding modularity
        of the newly created communities.
        """

        if self.verbose_mode:
            print(f'I was given: {rows=}')

        partition = []
        Q = 0

        n_rows = len(rows) 
        #print(f"Number of cells: {n_rows}")

        #If the number of rows is less than 3,
        #we keep the cluster as it is.
        if n_rows < 3:
            return (Q, partition)

        B = self.X[rows,:]
        ones = np.ones(n_rows)
        partial_row_sums = B.T.dot(ones)
        #1^T @ B @ B^T @ 1 = (B^T @ 1)^T @ (B^T @ 1)
        L_all = partial_row_sums @ partial_row_sums - n_rows
        #These are the row sums of the similarity matrix
        row_sums = B @ partial_row_sums
        #Check if we have negative entries before computing
        #the square root.
        # if  neg_row_sums or self.use_hermitian_method:
        zero_row_sums_mask = np.abs(row_sums) < self.eps
        has_zero_row_sums = zero_row_sums_mask.any()
        has_neg_row_sums = (row_sums < -self.eps).any() 

        if has_zero_row_sums:
            print("We have zero row sums.")
            row_sums[zero_row_sums_mask] = 0

        if has_neg_row_sums and has_zero_row_sums:
            txt = "This matrix cannot be processed."
            print(txt)
            txt = "Cannot have negative and zero row sums."
            raise ValueError(txt)

        if  has_neg_row_sums:
            #This means we cannot use the fast approach
            #We'll have to build a dense representation
            # of the similarity matrix.
            if 5000 < n_rows:
                print("The row sums are negative.")
                print("We will use a general solver.")
                print(f"The block size is {n_rows}.")
                print("Warning ...")
                txt = "This operation is very expensive."
                print(txt)
            laplacian_mtx  = B @ B.T
            row_sums_mtx   = sp.diags(row_sums)
            laplacian_mtx  = row_sums_mtx - laplacian_mtx

            E_obj = EigenGeneral(laplacian_mtx,
                                 k=2,
                                 M=row_sums_mtx,
                                 sigma=0,
                                 which="LM")
            eigen_val_abs = np.abs(E_obj[0])
            #Identify the eigenvalue with the
            #largest magnitude.
            idx = np.argmax(eigen_val_abs)
            #Choose the eigenvector corresponding
            # to the eigenvalue with the 
            # largest magnitude.
            eigen_vectors = E_obj[1]
            W = eigen_vectors[:,idx]

        elif self.use_hermitian_method or has_zero_row_sums:
            laplacian_mtx  = B @ B.T
            row_sums_mtx   = sp.diags(row_sums)
            laplacian_mtx  = row_sums_mtx - laplacian_mtx
            try:
                #if the row sums are negative, this 
                #step could fail.
                E_obj = EigenHermitian(laplacian_mtx,
                                        k=2,
                                        M=row_sums_mtx,
                                        sigma=0,
                                        which="LM")
                eigen_val_abs = np.abs(E_obj[0])
                #Identify the eigenvalue with the
                #largest magnitude.
                idx = np.argmax(eigen_val_abs)
                #Choose the eigenvector corresponding
                # to the eigenvalue with the 
                # largest magnitude.
                eigen_vectors = E_obj[1]
                W = eigen_vectors[:,idx]
            except:
                #This is a very expensive operation
                #since it computes all the eigenvectors.
                if 5000 < n_rows:
                    print("We will use a full eigen decomp.")
                    print(f"The block size is {n_rows}.")
                    print("Warning ...")
                    txt = "This operation is very expensive."
                    print(txt)

                E_obj = EigenGeneral(laplacian_mtx,
                                     k=2,
                                     M=row_sums_mtx,
                                     sigma=0,
                                     which="LM")
                eigen_val_abs = np.abs(E_obj[0])
                #Identify the eigenvalue with the
                #largest magnitude.
                idx = np.argmax(eigen_val_abs)
                #Choose the eigenvector corresponding
                # to the eigenvalue with the 
                # largest magnitude.
                eigen_vectors = E_obj[1]
                W = eigen_vectors[:,idx]

        else:
            #This is the fast approach.
            #It is fast in the sense that the 
            #operations are faster if the matrix
            #is sparse, i.e., O(n) nonzero entries.

            d = 1/np.sqrt(row_sums)
            D = sp.diags(d)
            C = D @ B
            W = self.trunc_SVD.fit_transform(C)
            singular_values = self.trunc_SVD.singular_values_
            idx = np.argsort(singular_values)
            #Get the singular vector corresponding to the
            #second largest singular value.
            W = W[:,idx[0]]


        mask_c1 = 0 < W
        mask_c2 = ~mask_c1

        #If one partition has all the elements
        #then return with Q = 0.
        if mask_c1.all() or mask_c2.all():
            return (Q, partition)

        masks = [mask_c1, mask_c2]

        for mask in masks:
            n_rows_msk = mask.sum()
            partition.append(rows[mask])
            ones_msk = ones * mask
            row_sums_msk = B.T.dot(ones_msk)
            O_c = row_sums_msk @ row_sums_msk - n_rows_msk
            L_c = ones_msk @ row_sums  - n_rows_msk
            Q += O_c / L_all - (L_c / L_all)**2

        if self.verbose_mode:
            print(f'{Q=}')
            print(f'I found: {partition=}')
            print('===========================')

        return (Q, partition)

    #=====================================
    def compute_partition_for_full(self, 
                                  rows: np.ndarray,
    ) -> Tuple[float, np.ndarray]:
        """
        Compute the partition of the given set
        of cells. The rows input
        contains the indices of the
        rows we are to partition.
        We assume that the matrix is full.
        For sparse matrices consider 
        compute_partition_for_sp()
        """

        if self.verbose_mode:
            print(f"I was given: {rows=}")

        partition = []
        Q = 0

        n_rows = len(rows) 
        #print(f"Number of cells: {n_rows}")

        #If the number of rows is less than 3,
        #we keep the cluster as it is.
        if n_rows < 3:
            return (Q, partition)

        S              = self.X[np.ix_(rows, rows)]
        ones           = np.ones(n_rows)
        row_sums       = S.dot(ones)
        row_sums_mtx   = sp.diags(row_sums)
        laplacian_mtx  = row_sums_mtx - S
        L_all = np.sum(row_sums) - n_rows

        zero_row_sums_mask = np.abs(row_sums) < self.eps
        has_zero_row_sums = zero_row_sums_mask.any()
        has_neg_row_sums = (row_sums < -self.eps).any() 

        if has_neg_row_sums:
            print("The similarity matrix "
                  "has negative row sums")

        if has_zero_row_sums:
            print("We have zero row sums.")
            row_sums[zero_row_sums_mask] = 0

        if has_neg_row_sums and has_zero_row_sums:
            txt = "This matrix cannot be processed."
            print(txt)
            txt = "Cannot have negative and zero row sums."
            raise ValueError(txt)

        if has_neg_row_sums:
            #This is a very expensive operation
            #since it computes all the eigenvectors.
            if 5000 < n_rows:
                print("The row sums are negative.")
                print("We will use a general solver.")
                print(f"The block size is {n_rows}.")
                print("Warning ...")
                txt = "This operation is very expensive."
                print(txt)
            E_obj = EigenGeneral(laplacian_mtx,
                                 k=2,
                                 M=row_sums_mtx,
                                 sigma=0,
                                 which="LM")
            eigen_val_abs = np.abs(E_obj[0])
            #Identify the eigenvalue with the
            #largest magnitude.
            idx = np.argmax(eigen_val_abs)
            #Choose the eigenvector corresponding
            # to the eigenvalue with the 
            # largest magnitude.
            eigen_vectors = E_obj[1]
            W = eigen_vectors[:,idx]
        else:
            #Nonnegative row sums.
            try:
                # print("Using the eigsh function.")

                E_obj = EigenHermitian(laplacian_mtx,
                                        k=2,
                                        M=row_sums_mtx,
                                        sigma=0,
                                        which="LM")
                eigen_val_abs = np.abs(E_obj[0])
                #Identify the eigenvalue with the
                #largest magnitude.
                idx = np.argmax(eigen_val_abs)
                #Choose the eigenvector corresponding
                # to the eigenvalue with the 
                # largest magnitude.
                eigen_vectors = E_obj[1]
                W = eigen_vectors[:,idx]

            except:
                # print("Using the eig function.")

                #This is a very expensive operation
                #since it computes all the eigenvectors.
                if 5000 < n_rows:
                    print("We will use a full eigen decomp.")
                    print(f"The block size is {n_rows}.")
                    print("Warning ...")
                    txt = "This operation is very expensive."
                    print(txt)

                E_obj = EigenGeneral(laplacian_mtx,
                                    k=2,
                                    M=row_sums_mtx,
                                    sigma=0,
                                    which="LM")
                eigen_val_abs = np.abs(E_obj[0])
                #Identify the eigenvalue with the
                #largest magnitude.
                idx = np.argmax(eigen_val_abs)
                #Choose the eigenvector corresponding
                # to the eigenvalue with the 
                # largest magnitude.
                eigen_vectors = E_obj[1]
                W = eigen_vectors[:,idx]

        mask_c1 = 0 < W
        mask_c2 = ~mask_c1

        #If one partition has all the elements
        #then return with Q = 0.
        if mask_c1.all() or mask_c2.all():
            return (Q, partition)

        masks = [mask_c1, mask_c2]

        for mask in masks:
            n_rows_msk = mask.sum()
            partition.append(rows[mask])
            ones_msk = ones * mask
            row_sums_msk = S @ ones_msk
            O_c = ones_msk @ row_sums_msk - n_rows_msk
            L_c = ones_msk @ row_sums  - n_rows_msk
            Q += O_c / L_all - (L_c / L_all)**2

        if self.verbose_mode:
            print(f'{Q=}')
            print(f'I found: {partition=}')
            print('===========================')

        return (Q, partition)

    #=====================================
    def compute_diameter_for_observations(
        self,
        matrix: ArrayLike,
        lp_norm: str = "l2",
        use_convex_hull: bool = False,
        use_brute_force: bool = False,
    ) -> float:
        """
        Assuming every row vector is a 
        point in R^n, we estimate the diameter 
        of that set.
        """

        if lp_norm == "l2":
            lp_norm_int = 2
        elif lp_norm == "l1":
            lp_norm_int = 1

        if use_convex_hull:
            indices = spatial.ConvexHull(matrix).vertices
            candidates = matrix[indices]
            distance_matrix = spatial.distance_matrix(
                candidates,
                candidates,
                p = lp_norm_int
            )
            return distance_matrix.max()

        if use_brute_force:
            distance_matrix = pairwise_distances(
                matrix,
                metric=lp_norm,
                #n_jobs=-1,
            )
            return distance_matrix.max()

        centroid  = matrix.mean(axis=0)
        max_norm = 0

        for row in matrix:

            dist = np.linalg.norm(row-centroid,
                                  ord=lp_norm_int)
            if max_norm < dist:
                max_norm = dist
        
        diameter =  2 * max_norm

        return diameter

    #=====================================

    # def linear_operator(
    #     self,
    #     matrix: ArrayLike,
    #     lp_norm: str,
    #     use_convex_hull: bool = False,
    # ) -> float:
    #     """
    #     """
    #=====================================
    def compute_partition_for_normsp(self,
                                    rows: np.ndarray,
    ) -> Tuple[float, np.ndarray]:
        """
        Compute the partition of the given set
        of cells. The rows input
        contains the indices of the
        rows we are to partition.
        We assume that the matrix is full.
        For sparse matrices consider 
        compute_partition_for_sp()
        """

        if self.verbose_mode:
            print(f"I was given: {rows=}")

        partition = []
        Q = 0

        n_rows = len(rows) 
        #print(f"Number of cells: {n_rows}")

        #If the number of rows is less than 3,
        #we keep the cluster as it is.
        if n_rows < 3:
            return (Q, partition)

        # S              = self.X[np.ix_(rows, rows)]
        similarity_op  = self.generate_norm_sim_as_linear_op(
            rows)
        ones           = np.ones(n_rows)
        row_sums       = similarity_op @ ones
        # row_sums_mtx   = sp.diags(row_sums)
        row_sums_op    = self.generate_diag_as_linear_op(
            row_sums)
        laplacian_op  = row_sums_op - similarity_op
        L_all = np.sum(row_sums) - n_rows

        zero_row_sums_mask = np.abs(row_sums) < self.eps
        has_zero_row_sums = zero_row_sums_mask.any()
        has_neg_row_sums = (row_sums < -self.eps).any() 

        if has_neg_row_sums:
            print("The similarity matrix "
                  "has negative row sums")

        if has_zero_row_sums:
            print("We have zero row sums.")
            row_sums[zero_row_sums_mask] = 0

        if has_neg_row_sums and has_zero_row_sums:
            txt = "This matrix cannot be processed."
            print(txt)
            txt = "Cannot have negative and zero row sums."
            raise ValueError(txt)

        if has_neg_row_sums:
            #This is a very expensive operation
            #since it computes all the eigenvectors.
            if 5000 < n_rows:
                print("The row sums are negative.")
                print("We will use a general solver.")
                print(f"The block size is {n_rows}.")
                print("Warning ...")
                txt = "This operation is very expensive."
                print(txt)
            E_obj = EigenGeneral(laplacian_op,
                                 k=2,
                                 M=row_sums_op,
                                 sigma=0,
                                 which="LM")
            eigen_val_abs = np.abs(E_obj[0])
            #Identify the eigenvalue with the
            #largest magnitude.
            idx = np.argmax(eigen_val_abs)
            #Choose the eigenvector corresponding
            # to the eigenvalue with the 
            # largest magnitude.
            eigen_vectors = E_obj[1]
            W = eigen_vectors[:,idx]
        else:
            #Nonnegative row sums.
            try:
                # print("Using the eigsh function.")

                E_obj = EigenHermitian(laplacian_op,
                                        k=2,
                                        M=row_sums_op,
                                        sigma=0,
                                        which="LM")
                eigen_val_abs = np.abs(E_obj[0])
                #Identify the eigenvalue with the
                #largest magnitude.
                idx = np.argmax(eigen_val_abs)
                #Choose the eigenvector corresponding
                # to the eigenvalue with the 
                # largest magnitude.
                eigen_vectors = E_obj[1]
                W = eigen_vectors[:,idx]

            except:
                # print("Using the eig function.")

                #This is a very expensive operation
                #since it computes all the eigenvectors.
                if 5000 < n_rows:
                    print("We will use a full eigen decomp.")
                    print(f"The block size is {n_rows}.")
                    print("Warning ...")
                    txt = "This operation is very expensive."
                    print(txt)

                E_obj = EigenGeneral(laplacian_op,
                                    k=2,
                                    M=row_sums_op,
                                    sigma=0,
                                    which="LM")
                eigen_val_abs = np.abs(E_obj[0])
                #Identify the eigenvalue with the
                #largest magnitude.
                idx = np.argmax(eigen_val_abs)
                #Choose the eigenvector corresponding
                # to the eigenvalue with the 
                # largest magnitude.
                eigen_vectors = E_obj[1]
                W = eigen_vectors[:,idx]

        mask_c1 = 0 < W
        mask_c2 = ~mask_c1

        #If one partition has all the elements
        #then return with Q = 0.
        if mask_c1.all() or mask_c2.all():
            return (Q, partition)

        masks = [mask_c1, mask_c2]

        for mask in masks:
            n_rows_msk = mask.sum()
            partition.append(rows[mask])
            ones_msk = ones * mask
            row_sums_msk = similarity_op @ ones_msk
            O_c = ones_msk @ row_sums_msk - n_rows_msk
            L_c = ones_msk @ row_sums  - n_rows_msk
            # Modularity
            Q += O_c / L_all - (L_c / L_all)**2

        if self.verbose_mode:
            print(f'{Q=}')
            print(f'I found: {partition=}')
            print('===========================')

        return (Q, partition)


    #=====================================
    def generate_norm_sim_as_linear_op(self,
                                    rows: np.ndarray,
    ) -> LinearOperator:
        """
        Generate a linear operator that describes
        the matrix:
        1 - ||x-y||^2 / ||x-y||^2_{max}
        """
        n_rows = len(rows)
        ones   = np.ones(n_rows)
        B = self.X[rows,:]
        norms = self.norm_sq_vec[rows]

        def mat_vec_prod(vec: ArrayLike):
            norm_sq = norms * (ones @ vec)
            norm_sq += ones * (norms @ vec)
            norm_sq -= 2 * B @ (B.T @ vec)
            norm_sq *= self.inv_diam_sq
            return ones * (ones @ vec) - norm_sq

        return LinearOperator(dtype=self.FDT,
                              shape=(n_rows,n_rows),
                              matvec=mat_vec_prod,
                              rmatvec=mat_vec_prod,
                              )

    #=====================================
    def generate_diag_as_linear_op(self,
                                    diagonal: np.ndarray,
    ) -> LinearOperator:
        """
        Generate a linear operator that describes
        the diagonal matrix:
        A(i,i) = vec(i)
        """

        n_rows = len(diagonal)
        D      = sp.diags(diagonal)
        def mat_vec_prod(vec: ArrayLike):
            return D @ vec

        return LinearOperator(dtype=self.FDT,
                              shape=(n_rows,n_rows),
                              matvec=mat_vec_prod,
                              rmatvec=mat_vec_prod,
                              )




