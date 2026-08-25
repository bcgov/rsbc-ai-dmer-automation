// naming.bicep
//
// Purpose: Naming convention helper functions (resource name generation from
// type/service/env/region/instance). See docs/standards/naming-conventions.md
// and docs/architecture/repository-design.md §12 for the pattern definitions.
//
// This module exports pure functions only — it declares no resources and is
// consumed by other modules/main.bicep via a compile-time `import`, e.g.:
//
//   import { resourceName, storageAccountName, regionAbbreviation } from '../shared/naming.bicep'

@description('Standard dashed pattern: <abbreviation>-rsbc-dmer-<service>-<environment>-<instance>')
@export()
func resourceName(abbreviation string, service string, environment string, instance string) string =>
  '${abbreviation}-rsbc-dmer-${service}-${environment}-${instance}'

@description('Maps an Azure region display name to the short abbreviation used in resource names.')
@export()
func regionAbbreviation(location string) string =>
  location == 'canadacentral'
    ? 'cac'
    : (location == 'canadaeast' ? 'cae' : location)

@description('Storage account name: no dashes, lowercase, <=24 chars — st + dmer + environment + region + instance.')
@export()
func storageAccountName(environment string, location string, instance string) string =>
  toLower('stdmer${environment}${regionAbbreviation(location)}${instance}')
