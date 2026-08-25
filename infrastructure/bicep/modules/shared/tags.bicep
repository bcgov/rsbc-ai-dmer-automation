// tags.bicep
//
// Purpose: Standard tag set helper (environment, service, cost-center, owner,
// data-classification). Exported as a pure function, consumed via a
// compile-time `import`, e.g.:
//
//   import { buildTags } from '../shared/tags.bicep'

@description('Builds the standard tag set applied to every resource in this repository.')
@export()
func buildTags(
  environment string,
  service string,
  costCenter string,
  owner string,
  dataClassification string
) object => {
  project: 'dmer-automation'
  environment: environment
  service: service
  costCenter: costCenter
  owner: owner
  dataClassification: dataClassification
}
