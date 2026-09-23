# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

output "app_service_account_email" {
  description = "Application service account email"
  value       = google_service_account.app_sa.email
}

output "logs_bucket_name" {
  description = "Logs storage bucket name"
  value       = google_storage_bucket.logs_data_bucket.name
}

output "cloudsql_instance_connection_name" {
  description = "Cloud SQL PostgreSQL instance connection name mounted at /cloudsql in Cloud Run"
  value       = google_sql_database_instance.postgres.connection_name
}

output "task_db_url_secret_id" {
  description = "Secret Manager secret ID storing the SQLAlchemy asyncpg TASK_DB_URL"
  value       = google_secret_manager_secret.task_db_url.secret_id
}
