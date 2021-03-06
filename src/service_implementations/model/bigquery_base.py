from pandas import DataFrame
from google.cloud import bigquery
from mixin.bigquery_mixin import BigQueryMixin
from helpers.bigquery_location import BigQueryLocation
from service_interfaces.model_interface import ModelOperatorInterface


class ModelOperatorBigQueryLocation(ModelOperatorInterface, BigQueryMixin):
    create_model_query = """
CREATE OR REPLACE MODEL `{sql_model_path}`
OPTIONS (
{options})
AS (
    WITH train_query AS ({train_query}),
         eval_data AS ({eval_data})
    SELECT *, false as is_eval FROM train_query UNION ALL 
    SELECT *, true as is_eval FROM eval_data
)
"""
    predict_query = """
SELECT {id_column}, predicted_{target_column} FROM ML.PREDICT(MODEL `{sql_model_path}`, ({predict_query}))
"""
    train_metric_query = """
SELECT * FROM ML.TRAINING_INFO(MODEL `{sql_model_path}`) ORDER BY iteration
    """
    train_feature_importance_query = """
SELECT * FROM ML.FEATURE_IMPORTANCE (MODEL `{sql_model_path}`)
    """
    job_id_prefix = 'mlflow_model_'

    def __init__(self, project, dataset):
        self.project = project
        self.dataset = dataset
        self.client = bigquery.Client()

    def instantiate_model(self, model_id, model_version, **kwargs) -> None:
        self.model_id = model_id
        self.model_version = model_version
        self.model_id_version = f'{self.model_id}_{self.model_version}'
        self.sql_model_path = f'{self.project}.{self.dataset}.{self.model_id_version}'
        self.kwargs = kwargs
        if not 'data_split_method'.lower() in [key.lower() for key in kwargs.keys()]:
            self.kwargs['data_split_method'] = 'CUSTOM'
            self.kwargs['data_split_col'] = 'is_eval'

    def fit(self, train_x, train_y, eval_x, eval_y, *args, **kwargs) -> None:
        self.kwargs['input_label_cols'] = train_y.columns
        quote = "'"
        options = ',\n'.join(
            [f'{key}={quote if isinstance(value, str) else ""}{value}{quote if isinstance(value, str) else ""}'
             for key, value in self.kwargs.items()])
        train_data_location = BigQueryLocation([*train_x.columns, *train_y.columns],
                                               train_x.id_column,
                                               train_x.table,
                                               train_x.order,
                                               train_x.limit)
        eval_data_location = BigQueryLocation([*eval_x.columns, *eval_y.columns],
                                              eval_x.id_column,
                                              eval_x.table,
                                              eval_x.order,
                                              eval_x.limit)
        create_model_query = self.create_model_query.format(
            train_query=train_data_location.get_select_query(),
            eval_data=eval_data_location.get_select_query(),
            sql_model_path=self.sql_model_path,
            options=options)
        query_job = self.run_query_and_wait(
            create_model_query, job_id_prefix=self.job_id_prefix)

    def predict(self, data: str, *args, **kwargs) -> None:
        target_column = self.kwargs['input_label_cols'][0]
        predict_query = self.predict_query.format(id_column=data.id_column,
                                                  target_column=target_column,
                                                  sql_model_path=self.sql_model_path,
                                                  predict_query=data.get_select_query(include_id=True))
        query_job = self.run_query_and_wait(predict_query,
                                            job_id_prefix=self.job_id_prefix)
        destination = query_job.destination
        destination_location = BigQueryLocation(columns=[f'predicted_{target_column}'],
                                                id_column=data.id_column,
                                                table=f'{destination.project}.{destination.dataset_id}.{destination.table_id}',
                                                order=data.order)
        return destination_location

    def save(self, folder_path: str, *args, **kwargs) -> None:
        pass

    def load(self, folder_path: str, *args, **kwargs) -> None:
        pass

    def get_train_metrics(self, *args, **kwargs) -> 'list[dict]':
        """Return a list of dictionaries to log in metrics MLFlow server

        Returns:
            list[dict]: E.g.
            [
                {
                metrics: {
                    validadtion_1.metric_1: value,
                    validadtion_1.metric_2: value,
                    validadtion_2.metric_1: value,
                    validadtion_2.metric_2: value
                    },
                step: 1
                },
                                {
                metrics: {
                    validadtion_1.metric_1: value,
                    validadtion_1.metric_2: value,
                    validadtion_2.metric_1: value,
                    validadtion_2.metric_2: value
                    },
                step: 2
                }
            ]
        """
        metrics_struct_list = []
        train_metric_query = self.train_metric_query.format(
            sql_model_path=self.sql_model_path)
        query_job = self.run_query_and_wait(train_metric_query,
                                            job_id_prefix=self.job_id_prefix)
        rows = query_job.result()

        for row in rows:
            row_metric = {
                'loss': row.loss,
                'eval_loss': row.eval_loss,
                'duration_ms': row.duration_ms,
                'learning_rate': row.learning_rate
            }
            metrics_struct_list.append({
                'metrics': row_metric,
                'step': row.iteration
            })

        return metrics_struct_list
